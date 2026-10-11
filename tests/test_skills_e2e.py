"""The whole loop, end to end: example -> draft -> card -> approval -> answer.

This is the test that proves the pieces fit. It runs the REAL store, the REAL
manifest validation, the REAL consent queue, the REAL door registration and the
REAL fastpath; the only two things standing in are the ones that need a Linux
box: the bubblewrap jail (replaced by ``skillkit.fake_execute``, which runs the
skill in process) and the socket (replaced by a fake ``egress_request`` that
records what it was asked for).

What the substitutes must NOT hide is asserted directly: the door is consulted
before any request, the request goes out through ``egress_request`` and through
nothing else, and it carries the skill's own door id.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from puffpai.ha_bridge import HUMAN_CONFIRMED                  # noqa: E402
from puffpai.security import consent_requests as creq          # noqa: E402
from puffpai.security import egress_doors                      # noqa: E402
from puffpai.skills import dispatch, install, sandbox, store    # noqa: E402
from puffpai import api_server                                 # noqa: E402
import skillkit                                                # noqa: E402

ENTITY = "skill:iss_position"

CANNED = {"name": "iss", "id": 25544, "latitude": 37.148923,
          "longitude": -122.874382, "altitude": 421.63, "velocity": 27583.4}


class FakeResponse:
    status_code = 200
    headers: dict = {}

    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def iter_content(self, size):
        yield self.body

    def close(self):
        pass


@pytest.fixture
def box(tmp_path, monkeypatch):
    """An appliance with skills switched on and a stand-in for the jail."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(creq, "REQUESTS_PATH",
                        str(tmp_path / "consent_requests.json"), raising=False)
    store.set_root(str(tmp_path / "skills"))
    install.reset_for_tests()
    egress_doors.clear_skill_doors()

    monkeypatch.setattr(sandbox, "sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox, "execute", skillkit.fake_execute)

    # The kill switch answered yes for this box. Patched at the dispatch layer
    # rather than by faking a whole config object: api_server reads the real
    # config at import time, and a stub would break it. The config path itself
    # is covered by test_skills_registry's env/config tests.
    monkeypatch.setattr(dispatch, "enabled", lambda: True)

    calls = []

    def _fake_egress_request(door_id, method, url, **kwargs):
        calls.append((door_id, method, url))
        return FakeResponse(CANNED)

    monkeypatch.setattr(egress_doors, "egress_request", _fake_egress_request)
    yield calls
    store.set_root(None)
    egress_doors.clear_skill_doors()
    from puffpai import capability_registry as cr
    cr.clear_skill_overlay()


def test_the_whole_loop(box):
    calls = box

    # 1. stage the shipped example as a draft. Staging is not installing.
    staged = install.stage_example("iss_position")
    assert staged["ok"] and store.installed_ids() == []

    # 2. the card: the exact bytes, hashed, with the domain named plainly.
    card = install.draft_bytes("iss_position")
    assert card["domains"] == ["wheretheiss.at"]
    assert card["lint"] == []
    names = {f["name"] for f in card["files"]}
    assert names == {"manifest.json", "skill.py", "tests_fixtures.json"}
    code = next(f for f in card["files"] if f["name"] == "skill.py")
    assert "def run(inputs, ctx):" in code["text"]
    assert len(code["sha256"]) == 64

    # 3. propose: the skill's own fixtures run OFFLINE first, in fixture mode.
    proposed = install.propose_install("iss_position", requested_by="owner")
    assert proposed["ok"] and proposed["awaiting_approval"]
    assert calls == [], "the offline test run reached the network"
    [request] = [r for r in creq._load() if r["action"] == "skill_install"]
    assert request["context"]["test_ok"] is True
    assert request["context"]["sha256"]["skill.py"] == code["sha256"]

    # 4. the owner approves on the dashboard.
    resolved = creq.resolve(ENTITY, approved=True, by="user_via_ui")
    installed = install.apply_approved_install(request=resolved,
                                               human_confirmed=HUMAN_CONFIRMED)
    assert installed["ok"] and installed["test_ok"] and installed["door_opened"]
    assert store.installed_ids() == ["iss_position"]

    # 5. the registry now knows about it, and the door is open for THIS host.
    from puffpai import capability_registry as cr
    row = cr.get("skill.iss_position")
    assert row is not None and cr.runtime_status(row) == cr.LIVE
    door_id = egress_doors.skill_door_id("iss_position", 0)
    allowed, reason = egress_doors.egress_allowed(door_id)
    assert allowed is True, reason

    # 6. "where is the ISS?" -- through the real fastpath.
    answer = api_server._try_skill_dispatch("Where is the ISS?")
    assert answer is not None
    assert answer.startswith("The ISS is at 37.1, -122.9, about 422 km up")
    assert "27583" in answer

    # ...and exactly one request left the house, through the door.
    assert calls == [(door_id, "GET",
                      "https://api.wheretheiss.at/v1/satellites/25544")]

    # 7. a sentence that is not the trigger is not answered by the skill.
    assert api_server._try_skill_dispatch("where is the iss headed next week") is None
    assert api_server._try_skill_dispatch("turn off the lights") is None

    # 8. "what skills do I have"
    listed = api_server._try_skills_list("what skills do I have")
    assert "ISS position" in listed and "wheretheiss.at" in listed

    # 9. "remove the ISS position skill" -- asks first. The clarifier is keyed
    # on the conversation, so the question and the answer must be in one.
    from puffpai import pending_clarifier, turn_context
    turn_context.begin("conv-skills-e2e")
    asked = api_server._try_skill_remove("remove the ISS position skill")
    assert asked and asked.startswith("Remove the ISS position skill?")
    assert store.installed_ids() == ["iss_position"], "removed without asking"

    done = pending_clarifier.resolve("yes", "conv-skills-e2e")
    assert done and "gone" in done
    assert store.installed_ids() == []
    assert egress_doors.door(door_id) is None
    assert cr.get("skill.iss_position") is None


def test_a_shut_door_stops_the_skill_and_switches_it_off(box, monkeypatch):
    """The fail-shut path: the owner closes the door, the skill asks anyway."""
    calls = box
    install.stage_example("iss_position")
    install.propose_install("iss_position", requested_by="owner")
    resolved = creq.resolve(ENTITY, approved=True, by="user_via_ui")
    assert install.apply_approved_install(request=resolved,
                                          human_confirmed=HUMAN_CONFIRMED)["ok"]

    monkeypatch.setattr(egress_doors, "egress_allowed",
                        lambda _door: (False, "the owner closed it"))
    outcome = dispatch.run("iss_position")
    assert outcome["ok"] is False
    assert "permission" in outcome["speech"]
    assert calls == [], "a request went out through a shut door"
    assert store.read_state("iss_position")["enabled"] is False, (
        "a door refusal must switch the skill off, not leave it retrying")


def test_the_kill_switch_stops_dispatch_even_for_an_installed_skill(box,
                                                                    monkeypatch):
    install.stage_example("iss_position")
    install.propose_install("iss_position", requested_by="owner")
    resolved = creq.resolve(ENTITY, approved=True, by="user_via_ui")
    install.apply_approved_install(request=resolved,
                                   human_confirmed=HUMAN_CONFIRMED)

    monkeypatch.setenv(dispatch.DISABLE_ENV, "1")
    outcome = dispatch.run("iss_position")
    assert outcome["ok"] is False and dispatch.DISABLE_ENV in outcome["error"]

    assert api_server._try_skill_dispatch("where is the iss") is None


def test_a_tampered_skill_refuses_and_disables_itself(box):
    install.stage_example("iss_position")
    install.propose_install("iss_position", requested_by="owner")
    resolved = creq.resolve(ENTITY, approved=True, by="user_via_ui")
    install.apply_approved_install(request=resolved,
                                   human_confirmed=HUMAN_CONFIRMED)

    folder = Path(store.skill_dir("iss_position"))
    (folder / "skill.py").write_text(
        "def run(inputs, ctx):\n"
        "    return {'speech': 'mine now', 'data': {}}\n", encoding="utf-8")

    outcome = dispatch.run("iss_position")
    assert outcome["ok"] is False
    assert "isn't the code you approved" in outcome["speech"]
    assert store.read_state("iss_position")["enabled"] is False


# ── offline is not a refusal ──────────

OFFLINE_ISS = ("I couldn't reach the internet just now, so I can't get the "
               "ISS position.")


def _installed(box):
    install.stage_example("iss_position")
    install.propose_install("iss_position", requested_by="owner")
    resolved = creq.resolve(ENTITY, approved=True, by="user_via_ui")
    assert install.apply_approved_install(request=resolved,
                                          human_confirmed=HUMAN_CONFIRMED)["ok"]
    door_id = egress_doors.skill_door_id("iss_position", 0)
    assert egress_doors.egress_allowed(door_id)[0] is True
    return door_id


def _no_digits_no_exception_names(speech):
    assert not any(ch.isdigit() for ch in speech), speech
    for word in ("Error", "Timeout", "Exception", "unresolvable", "requests"):
        assert word not in speech, speech


def _raise_on_egress(monkeypatch, exc):
    calls = []

    def _boom(door_id, method, url, **kwargs):
        calls.append((door_id, method, url))
        raise exc
    monkeypatch.setattr(egress_doors, "egress_request", _boom)
    return calls


@pytest.mark.parametrize("make_exc", [
    lambda: __import__("requests").exceptions.ConnectionError("x"),
    lambda: __import__("requests").exceptions.ConnectTimeout("x"),
    lambda: egress_doors.EgressBlocked(
        "skill:iss_position:0",
        f"{egress_doors.REASON_UNRESOLVABLE}: host 'api.wheretheiss.at' does "
        f"not resolve", "could not look it up"),
], ids=["connection_error", "connect_timeout", "unresolvable"])
def test_offline_leaves_the_skill_on_and_says_one_plain_sentence(
        box, monkeypatch, make_exc):
    door_id = _installed(box)
    calls = _raise_on_egress(monkeypatch, make_exc())

    outcome = dispatch.run("iss_position")

    assert outcome["ok"] is False
    assert outcome["speech"] == OFFLINE_ISS
    _no_digits_no_exception_names(outcome["speech"])
    assert calls, "the request should have been attempted through the door"
    assert store.read_state("iss_position")["enabled"] is True
    assert egress_doors.egress_allowed(door_id)[0] is True, (
        "a lost connection must not touch the owner's door flag")


def test_the_chat_hook_says_the_offline_sentence_and_then_works_again(
        box, monkeypatch):
    _installed(box)
    requests = pytest.importorskip("requests")
    good = egress_doors.egress_request
    _raise_on_egress(monkeypatch, requests.exceptions.ConnectionError("x"))
    assert api_server._try_skill_dispatch("where is the iss") == OFFLINE_ISS

    monkeypatch.setattr(egress_doors, "egress_request", good)  # cable back
    answer = api_server._try_skill_dispatch("where is the iss")
    assert answer and answer.startswith("The ISS is at")


def test_a_server_error_is_not_offline_and_not_a_refusal(box, monkeypatch):
    door_id = _installed(box)

    class Busy(FakeResponse):
        status_code = 503

    monkeypatch.setattr(egress_doors, "egress_request",
                        lambda *a, **k: Busy(CANNED))
    outcome = dispatch.run("iss_position")
    assert "internet" not in outcome["speech"]
    assert "ISS position" in outcome["speech"]
    _no_digits_no_exception_names(outcome["speech"])
    assert store.read_state("iss_position")["enabled"] is True
    assert egress_doors.egress_allowed(door_id)[0] is True


def test_a_refusal_still_switches_the_skill_off(box, monkeypatch):
    door_id = _installed(box)
    _raise_on_egress(monkeypatch, egress_doors.EgressBlocked(
        door_id, f"{egress_doors.REASON_RESOLVED_PRIVATE} (private): points "
                 f"indoors", "points indoors"))
    outcome = dispatch.run("iss_position")
    assert "permission" in outcome["speech"]
    assert store.read_state("iss_position")["enabled"] is False
    assert egress_doors.egress_allowed(door_id)[0] is False


def test_the_generic_failure_line_speaks_no_raw_error(box, monkeypatch):
    _installed(box)

    class Garbled(FakeResponse):
        def __init__(self, payload):
            self.body = b'{"latitude": "not a number"}'

    monkeypatch.setattr(egress_doors, "egress_request",
                        lambda *a, **k: Garbled(CANNED))
    outcome = dispatch.run("iss_position")
    assert outcome["ok"] is False
    assert outcome["speech"] == "The ISS position skill didn't work just now."
    assert store.read_state("iss_position")["enabled"] is True


# ── a matched trigger never falls through to the model ──────────────

def test_a_disabled_skill_gets_an_honest_answer_not_the_model(box):
    calls = box
    _installed(box)
    store.set_enabled("iss_position", False, reason="test")
    answer = api_server._try_skill_dispatch("where is the iss")
    assert answer == ("The ISS position skill is switched off right now, so I "
                      "can't run it. You can turn it back on from the Skills "
                      "page.")
    assert calls == []
    assert store.read_state("iss_position")["enabled"] is False


def test_a_shut_door_gets_an_honest_answer_at_the_hook(box, monkeypatch):
    calls = box
    _installed(box)
    monkeypatch.setattr(egress_doors, "egress_allowed",
                        lambda _door: (False, "the owner closed it"))
    answer = api_server._try_skill_dispatch("where is the iss")
    assert answer and "isn't allowed to go online" in answer
    assert calls == []


def test_an_unrelated_sentence_still_falls_through(box):
    _installed(box)
    assert api_server._try_skill_dispatch("where is the remote") is None


# ── a fresh process: doors are in memory only ───────────────────────

def test_the_first_run_after_a_restart_registers_the_door_and_works(box):
    calls = box
    door_id = _installed(box)
    calls.clear()
    # A restart: the in-memory registry is empty, the owner's flag is still
    # true on disk, and the skill is still enabled.
    egress_doors.clear_skill_doors()
    assert egress_doors.door(door_id) is None

    outcome = dispatch.run("iss_position")

    assert outcome["ok"] is True, outcome
    assert calls == [(door_id, "GET",
                      "https://api.wheretheiss.at/v1/satellites/25544")]
    assert store.read_state("iss_position")["enabled"] is True
    assert egress_doors.egress_allowed(door_id)[0] is True


def test_an_unregistered_door_is_a_setup_fault_not_a_refusal(box, monkeypatch):
    calls = box
    door_id = _installed(box)
    calls.clear()
    egress_doors.clear_skill_doors()
    # Registration itself fails in this process.
    monkeypatch.setattr(store, "ensure_door_registered", lambda _sid: False)

    outcome = dispatch.run("iss_position")

    assert outcome["ok"] is False
    assert outcome["speech"] == ("Something went wrong on my side, so I can't "
                                 "get the ISS position right now.")
    assert calls == []
    assert store.read_state("iss_position")["enabled"] is True
    monkeypatch.undo()
    assert store.ensure_door_registered("iss_position") is True
    assert egress_doors.egress_allowed(door_id)[0] is True, (
        "the owner's flag must survive a registration fault")


def test_the_stale_flag_prune_keeps_an_enabled_skills_flag_before_registration(
        box):
    door_id = _installed(box)
    egress_doors.clear_skill_doors()
    from puffpai.security import egress_admin
    egress_admin.prune_skill_flags(store.entitled_door_flags())
    assert store.ensure_door_registered("iss_position") is True
    assert egress_doors.egress_allowed(door_id)[0] is True
