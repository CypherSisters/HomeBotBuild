"""What the appliance does about skills on a box with no bubblewrap.

Refusing to run skill code unsandboxed is correct; doing it SILENTLY is not.
On Windows, and on any Linux machine without ``/usr/bin/bwrap``, a skill can be
submitted and approved and then never run, and something has to say so.

Two invariants, both of them "the message and the state agree":

1. ``apply_approved_install`` runs the skill's test first, and the skill's
   egress door only opens if the test passed. Otherwise "installed but
   switched off" could sit over a skill that is ON, with an OPEN door, and only
   the ``last_test`` flag would hint otherwise.
2. ``enable_installed`` asks whether a jail exists, so the dashboard's enable
   button (and the chat "enable the X skill" path, which calls the same
   function) does not switch a skill on and reopen its door on a machine where
   no skill can run.

Every test in this file asserts against THIS box's real ``sandbox_available()``
— they are meaningful because the Windows dev box has no bubblewrap, and they
skip on a box that does, where ``test_skills_e2e`` covers the other side.

Run: pytest tests/test_skills_no_sandbox.py -q

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

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
from skillkit import stage_example_draft                       # noqa: E402

ENTITY = "skill:iss_position"
SKILL = "iss_position"
HOSTS = ["api.wheretheiss.at"]

needs_no_jail = pytest.mark.skipif(
    sandbox.sandbox_available(),
    reason="this box HAS bubblewrap; the sandboxed path is test_skills_e2e")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(creq, "REQUESTS_PATH",
                        str(tmp_path / "consent_requests.json"), raising=False)
    store.set_root(str(tmp_path / "skills"))
    install.reset_for_tests()
    egress_doors.clear_skill_doors()
    egress_doors.set_overlay_override(
        str(tmp_path / "own" / "egress_doors.json"))
    egress_doors.reset_cache()
    yield
    egress_doors.set_overlay_override(None)
    egress_doors.reset_cache()
    store.set_root(None)
    egress_doors.clear_skill_doors()
    try:
        from puffpai import capability_registry as cr
        cr.clear_skill_overlay()
    except Exception:
        pass


def skill_flags() -> dict:
    path = Path(egress_doors.overlay_path())
    if not path.exists():
        return {}
    doors = json.loads(path.read_text(encoding="utf-8")).get("doors", {})
    return {k: v for k, v in doors.items() if k.startswith("skill_")}


def propose():
    stage_example_draft(store)
    return install.propose_install(SKILL, requested_by="owner")


def install_it():
    propose()
    return install.apply_approved_install(
        request=creq.resolve(ENTITY, approved=True, by="user_via_ui"),
        human_confirmed=HUMAN_CONFIRMED)


# ===========================================================================
# 1. submission and proposal still work
# ===========================================================================

@needs_no_jail
def test_a_submission_still_works_with_no_sandbox():
    """Authoring is not running. A developer on a laptop can still get a card."""
    from puffpai.skills import submit as _submit

    from test_skills_submit import package_files

    out = _submit.submit_and_propose("tide_clock", package_files())
    assert out["submitted"] is True
    assert out["proposed"] is True and out["awaiting_approval"] is True
    assert store.installed_ids() == []


@needs_no_jail
def test_the_consent_card_says_the_tests_were_not_run():
    proposed = propose()
    assert proposed["ok"] is True and proposed["awaiting_approval"] is True
    [request] = [r for r in creq._load() if r["action"] == "skill_install"]
    assert request["context"]["test_ok"] is False
    reason = request["reason"]
    assert "could NOT run its tests" in reason
    assert "no sandbox" in reason
    assert "install switched off" in reason


# ===========================================================================
# 2. the install lands OFF, with no door
# ===========================================================================

@needs_no_jail
def test_installing_lands_switched_off_with_the_door_shut():
    out = install_it()
    assert out["ok"] is True and out["installed"] is True
    assert out["test_ok"] is False
    assert out["sandbox_unavailable"] is True
    assert out["door_opened"] is False
    assert install.NO_SANDBOX_INSTALL_SENTENCE in out["message"]

    # The disk agrees with the sentence: OFF, and no door flag was written.
    assert store.read_state(SKILL)["enabled"] is False
    assert skill_flags() == {}, "an untestable skill got an open door"
    door_id = egress_doors.skill_door_id(SKILL, 0)
    allowed, _reason = egress_doors.egress_allowed(door_id)
    assert allowed is False


@needs_no_jail
def test_the_capability_row_reads_offline_not_live():
    install_it()
    from puffpai import capability_probes as probes
    from puffpai import capability_registry as cr

    probes.reset_cache()
    row = cr.get(f"skill.{SKILL}")
    assert row is not None
    assert cr.runtime_status(row) == cr.OFFLINE


@needs_no_jail
def test_dispatch_refuses_to_run_it_at_all(monkeypatch):
    install_it()
    outcome = dispatch.run(SKILL)
    assert outcome["ok"] is False

    # ``available()`` has two "off" reasons and the config one wins on a stock
    # build. Answer the sandbox question on its own by saying yes to the other.
    monkeypatch.setattr(dispatch, "enabled", lambda: True)
    usable, reason = dispatch.available()
    assert usable is False and "no sandbox helper" in reason
    assert dispatch.run(SKILL)["ok"] is False


# ===========================================================================
# 3. enable refuses, in one plain sentence
# ===========================================================================

@needs_no_jail
def test_enable_refuses_with_one_plain_sentence():
    install_it()
    out = install.enable_installed(SKILL)
    assert out["ok"] is False
    assert out["status"] == 409
    assert out["error"] == "no_sandbox"
    assert out["message"] == install.NO_SANDBOX_ENABLE_SENTENCE
    assert out["message"].count(".") == 1, "more than one sentence"

    # Nothing moved: still off, still no door.
    assert store.read_state(SKILL)["enabled"] is False
    assert skill_flags() == {}


@needs_no_jail
def test_the_enable_route_refuses_too(monkeypatch):
    from puffpai import api_server as mod

    install_it()
    mod.app.config["TESTING"] = True
    mod._rate_limit_store.clear()
    with mod.app.test_client() as client:
        resp = client.post(f'/skills/{SKILL}/enable',
                           headers={"X-API-Key": mod._API_KEY})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False and body["error"] == "no_sandbox"
    assert body["message"] == install.NO_SANDBOX_ENABLE_SENTENCE
    assert store.read_state(SKILL)["enabled"] is False
    assert skill_flags() == {}


@needs_no_jail
def test_the_enable_route_still_404s_for_an_unknown_skill():
    """The sandbox refusal must not swallow the more useful answer."""
    from puffpai import api_server as mod

    mod.app.config["TESTING"] = True
    mod._rate_limit_store.clear()
    with mod.app.test_client() as client:
        resp = client.post('/skills/no_such_skill/enable',
                           headers={"X-API-Key": mod._API_KEY})
    assert resp.status_code == 404


# ===========================================================================
# 4. /capabilities/extend tells a person what to install
# ===========================================================================

def test_capabilities_extend_publishes_the_sandbox_requirements():
    from puffpai import api_server as mod

    mod.app.config["TESTING"] = True
    mod._rate_limit_store.clear()
    with mod.app.test_client() as client:
        body = client.get('/capabilities/extend',
                          headers={"X-API-Key": mod._API_KEY}).get_json()

    reqs = body["skills"]["sandbox_requirements"]
    assert reqs["required"] is True
    assert reqs["package"] == "bubblewrap"
    assert reqs["helper"] == sandbox.SKILL_BWRAP
    assert reqs["satisfied"] is sandbox.sandbox_available()
    assert reqs["install"]["debian"] == "sudo apt install bubblewrap"
    assert reqs["install"]["ubuntu"] == "sudo apt install bubblewrap"
    assert "not supported" in reqs["install"]["windows"]
    assert "WSL2" in reqs["install"]["windows"]
    assert "unsandboxed" in reqs["note"]
    assert body["skills"]["submit_route"] == "/skills/drafts"


@needs_no_jail
def test_capabilities_extend_is_honest_about_this_windows_box():
    from puffpai import api_server as mod

    mod.app.config["TESTING"] = True
    mod._rate_limit_store.clear()
    with mod.app.test_client() as client:
        body = client.get('/capabilities/extend',
                          headers={"X-API-Key": mod._API_KEY}).get_json()

    skills = body["skills"]
    assert skills["enabled"] is False
    assert skills["sandbox_available"] is False
    reqs = skills["sandbox_requirements"]
    assert reqs["satisfied"] is False
    assert reqs["supported"] is (not sys.platform.startswith("win"))
    assert reqs["this_box"], "no sentence about what to do on this box"
    if sys.platform.startswith("win"):
        assert "WSL2" in reqs["this_box"]
    else:
        assert "bubblewrap" in reqs["this_box"]


def test_no_weaker_fallback_sandbox_exists():
    """The one thing that must NOT exist: a weaker fallback sandbox.

    ``sandbox.execute`` refuses without the helper and there is no second code
    path: the module names exactly one helper, at an absolute path, and the
    only ``Popen`` in it is the bubblewrap argv.
    """
    source = (Path(__file__).resolve().parents[1] / "src" / "puffpai" /
              "skills" / "sandbox.py").read_text(encoding="utf-8")
    assert source.count("subprocess.Popen") == 1
    assert 'SKILL_BWRAP = "/usr/bin/bwrap"' in source
    for weasel in ("fallback", "unsandboxed=", "allow_unsandboxed",
                   "PUFFPAI_SKIP_SANDBOX", "nsjail", "firejail"):
        assert weasel not in source, weasel


@needs_no_jail
def test_execute_raises_rather_than_running_anything():
    with pytest.raises(sandbox.SandboxUnavailable) as exc:
        sandbox.execute("nowhere", {}, broker=None, run_root="nowhere",
                        timeout_s=1)
    assert "unsandboxed" in str(exc.value)
