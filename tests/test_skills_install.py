"""Propose, approve, install -- and the three gates that must all hold.

One test per gate, plus the two properties that make the gates worth having:
the install is an atomic rename (so there is no half-installed skill for a
dispatch to find), and the door flag that gets written is the digest of the
host set that was ON THE CARD.

The sha256 gate has a second half that is easy to miss and is tested here: a
mismatch does not merely refuse, it **voids** the approval. An owner's yes was
for particular bytes; leaving it spendable would let the next attempt --- with
the same, still-changed files --- inherit it.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from puffpai.ha_bridge import HUMAN_CONFIRMED                  # noqa: E402
from puffpai.security import consent_requests as creq          # noqa: E402
from puffpai.security import egress_doors                      # noqa: E402
from puffpai.skills import install, sandbox, store             # noqa: E402
import skillkit                                                # noqa: E402
from skillkit import stage_example_draft                       # noqa: E402

ENTITY = "skill:iss_position"


@pytest.fixture
def sandboxed(monkeypatch):
    """Stand in for the jail, so an install PASSES its test and opens its door.

    Needed since the install order was fixed: the offline fixture run now
    happens BEFORE the door is opened and a skill that cannot be tested here
    installs switched off with no door (see ``test_skills_no_sandbox.py``). Every
    test below that asserts an OPEN door is therefore a test about a box where
    skills can actually run, and says so by asking for this fixture.

    Same substitute ``test_skills_e2e`` uses, and it proves nothing about
    isolation -- the jail's own assertions live in ``test_skills_sandbox.py``.
    """
    monkeypatch.setattr(sandbox, "sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox, "execute", skillkit.fake_execute)
    yield


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    # PUFFPAI_DATA_DIR alone is not enough: consent_requests caches its data
    # dir in a module global on first use, so the SECOND test in a file would
    # otherwise read the first one's queue -- and "there is no approval on
    # record" is exactly the property these tests check.
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(creq, "REQUESTS_PATH",
                        str(tmp_path / "consent_requests.json"), raising=False)
    store.set_root(str(tmp_path / "skills"))
    install.reset_for_tests()
    egress_doors.clear_skill_doors()
    yield
    store.set_root(None)
    egress_doors.clear_skill_doors()
    try:
        from puffpai import capability_registry as cr
        cr.clear_skill_overlay()
    except Exception:
        pass


def propose():
    stage_example_draft(store)
    return install.propose_install("iss_position", requested_by="owner")


def approve():
    return creq.resolve(ENTITY, approved=True, by="user_via_ui")


# ── propose writes a card and NOTHING else ──────────────────────────

def test_propose_files_a_request_and_installs_nothing():
    result = propose()
    assert result["ok"] and result["awaiting_approval"]
    assert store.installed_ids() == []
    [req] = [r for r in creq._load() if r["action"] == "skill_install"]
    assert req["entity_id"] == ENTITY and req["status"] == "open"
    assert set(req["context"]["sha256"]) == {"manifest.json", "skill.py",
                                             "tests_fixtures.json"}


def test_the_card_names_the_registrable_domain_and_the_exact_host():
    propose()
    [req] = [r for r in creq._load() if r["action"] == "skill_install"]
    reason = req["reason"]
    assert "wheretheiss.at" in reason
    assert "api.wheretheiss.at" in reason
    for digest in req["context"]["sha256"].values():
        assert digest in reason, "the card must show the bytes it is approving"


def test_the_card_says_what_the_skill_cannot_reach():
    propose()
    [req] = [r for r in creq._load() if r["action"] == "skill_install"]
    for promise in ("no environment", "no Home Assistant", "vault"):
        assert promise in req["reason"]


def test_registrable_domain():
    assert install.registrable_domain("api.wheretheiss.at") == "wheretheiss.at"
    assert install.registrable_domain("a.b.example.co.uk") == "example.co.uk"
    assert install.registrable_domain("example.com") == "example.com"


# ── gate 1: the in-process identity token ───────────────────────────

def test_without_the_identity_token_nothing_installs():
    propose()
    approve()
    for spoof in (True, "HUMAN_CONFIRMED", 1, {"human": True}, None):
        out = install.apply_approved_install(human_confirmed=spoof)
        assert out["ok"] is False
        assert "human confirmation" in out["error"]
    assert store.installed_ids() == []


# ── gate 2: a fresh, human-resolved, unspent approval ───────────────

def test_without_an_approval_nothing_installs():
    propose()
    out = install.apply_approved_install(human_confirmed=HUMAN_CONFIRMED)
    assert out["ok"] is False and "no approved skill install" in out["error"]
    assert store.installed_ids() == []


def test_a_denial_is_not_an_approval():
    propose()
    creq.resolve(ENTITY, approved=False, by="user_via_ui")
    out = install.apply_approved_install(human_confirmed=HUMAN_CONFIRMED)
    assert out["ok"] is False
    assert store.installed_ids() == []


def test_an_approval_resolved_by_a_non_human_is_not_an_approval():
    propose()
    creq.resolve(ENTITY, approved=True, by="agent_auto")
    out = install.apply_approved_install(human_confirmed=HUMAN_CONFIRMED)
    assert out["ok"] is False
    assert store.installed_ids() == []


def test_an_approval_is_spent_once():
    propose()
    approve()
    first = install.apply_approved_install(human_confirmed=HUMAN_CONFIRMED)
    assert first["ok"] is True
    stage_example_draft(store)          # a second draft, same approval
    second = install.apply_approved_install(human_confirmed=HUMAN_CONFIRMED)
    assert second["ok"] is False


# ── gate 3: the bytes ───────────────────────────────────────────────

def test_a_file_changed_after_approval_refuses_and_voids_the_approval():
    propose()
    approved = approve()
    draft = Path(store.draft_dir("iss_position"))
    (draft / "skill.py").write_text(
        "def run(inputs, ctx):\n    return {'speech': 'evil', 'data': {}}\n",
        encoding="utf-8")

    out = install.apply_approved_install(request=approved,
                                         human_confirmed=HUMAN_CONFIRMED)
    assert out["ok"] is False and out["error"] == "sha256 mismatch"
    assert store.installed_ids() == []

    # ...and the approval is gone: putting the file back does not resurrect it.
    stage_example_draft(store)
    again = install.apply_approved_install(request=approved,
                                           human_confirmed=HUMAN_CONFIRMED)
    assert again["ok"] is False, "a voided approval was spendable again"
    assert store.installed_ids() == []


def test_a_manifest_edited_after_approval_is_refused():
    propose()
    approved = approve()
    draft = Path(store.draft_dir("iss_position"))
    manifest = json.loads((draft / "manifest.json").read_text(encoding="utf-8"))
    manifest["permissions"]["network_hosts"].append("evil.example.com")
    (draft / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    out = install.apply_approved_install(request=approved,
                                         human_confirmed=HUMAN_CONFIRMED)
    assert out["ok"] is False
    assert store.installed_ids() == []


# ── what a successful install actually does ─────────────────────────

def test_a_successful_install_moves_the_draft_and_records_the_approval():
    propose()
    approved = approve()
    out = install.apply_approved_install(request=approved,
                                         human_confirmed=HUMAN_CONFIRMED)
    assert out["ok"] is True and out["installed"] is True
    assert store.installed_ids() == ["iss_position"]
    assert not os.path.isdir(store.draft_dir("iss_position")), (
        "the draft survived the install; there must be exactly one copy")

    record = store.read_installed("iss_position")
    assert record["approved_by"] == "user_via_ui"
    assert record["approved_at"] == approved["resolved_at"]
    assert record["hosts"] == ["api.wheretheiss.at"]
    assert set(record["sha256"]) == {"manifest.json", "skill.py",
                                     "tests_fixtures.json"}
    ok, reason = store.integrity("iss_position")
    assert ok, reason


def test_the_door_flag_written_is_the_digest_of_the_approved_hosts(sandboxed):
    propose()
    out = install.apply_approved_install(request=approve(),
                                         human_confirmed=HUMAN_CONFIRMED)
    assert out["ok"] is True
    expected = egress_doors.skill_door_flag("iss_position", 0,
                                            ["api.wheretheiss.at"])
    overlay = json.loads(Path(egress_doors.overlay_path()).read_text(
        encoding="utf-8"))
    assert overlay["doors"].get(expected) is True, overlay
    # A different host set is a different key, and nothing opened it.
    other = egress_doors.skill_door_flag("iss_position", 0,
                                         ["api.wheretheiss.at",
                                          "evil.example.com"])
    assert other != expected
    assert other not in overlay["doors"]


def test_the_installed_skill_reports_its_door_shut_until_the_flag_matches(sandboxed):
    propose()
    install.apply_approved_install(request=approve(),
                                   human_confirmed=HUMAN_CONFIRMED)
    door_id = egress_doors.skill_door_id("iss_position", 0)
    allowed, reason = egress_doors.egress_allowed(door_id)
    assert allowed is True, reason

    # Re-register the door with an extra host (what an edited manifest would
    # do): the flag key changes and the owner's `true` does not follow it.
    egress_doors.register_skill_doors([("iss_position", [{
        "hosts": ["api.wheretheiss.at", "evil.example.com"],
        "purpose": "x", "data_sent_plain": "y"}])])
    allowed, reason = egress_doors.egress_allowed(door_id)
    assert allowed is False, "an edited host set inherited the owner's consent"


def test_installing_without_a_sandbox_leaves_the_skill_untested(tmp_path):
    """On a box with no bubblewrap the skill installs SWITCHED OFF rather than
    live-and-unrunnable. Fail closed, and say so."""
    from puffpai.skills import sandbox

    if sandbox.sandbox_available():
        pytest.skip("this box has a sandbox; see test_skills_e2e for that path")
    propose()
    out = install.apply_approved_install(request=approve(),
                                         human_confirmed=HUMAN_CONFIRMED)
    assert out["ok"] is True and out["test_ok"] is False
    assert "switched OFF" in out["message"]
    assert store.read_state("iss_position")["last_test"]["ok"] is False
    # And the disk agrees with the sentence: OFF, with no door. Fully
    # covered in tests/test_skills_no_sandbox.py.
    assert store.read_state("iss_position")["enabled"] is False
    assert out["door_opened"] is False


# ── enable, the code view, and the life of a door flag ────
#
# The flag is the only on-disk record of a yes. These pin the three ways it can
# stop being true (disable, remove, a rescan that finds the skill gone) and the
# ONE way it can become true again short of a fresh consent card: an enable
# that re-verifies the exact bytes and the exact reach that were approved.

APPROVED_HOSTS = ["api.wheretheiss.at"]


@pytest.fixture
def own_overlay(tmp_path):
    """This test's own door overlay, so a flag assertion is about THIS test.

    ``conftest`` points every test at one shared overlay seeded with the two
    Toolsmith doors; asserting "exactly one skill flag" against a file other
    tests also write would be asserting about the session, not about the code.
    """
    egress_doors.set_overlay_override(str(tmp_path / "state" / "egress_doors.json"))
    egress_doors.reset_cache()
    yield
    egress_doors.set_overlay_override(None)
    egress_doors.reset_cache()


def install_now():
    propose()
    out = install.apply_approved_install(request=approve(),
                                         human_confirmed=HUMAN_CONFIRMED)
    assert out["ok"] is True, out
    return out


def skill_flags():
    """Every ``skill_*`` key in the owner overlay, as it is on disk."""
    path = Path(egress_doors.overlay_path())
    if not path.exists():
        return {}
    doors = json.loads(path.read_text(encoding="utf-8")).get("doors", {})
    return {k: v for k, v in doors.items() if k.startswith("skill_")}


def approved_flag():
    return egress_doors.skill_door_flag("iss_position", 0, APPROVED_HOSTS)


def test_enable_turns_a_disabled_skill_back_on(own_overlay, sandboxed):
    install_now()
    assert skill_flags() == {approved_flag(): True}

    store.set_enabled("iss_position", False, reason="you asked me to")
    assert skill_flags() == {}, "switching a skill off left its door flag set"

    out = install.enable_installed("iss_position")
    assert out["ok"] is True and out["status"] == 200, out
    assert out["door_opened"] is True
    assert store.read_state("iss_position")["enabled"] is True
    assert skill_flags() == {approved_flag(): True}, (
        "enable did not re-assert exactly the approved digest's flag")
    allowed, why = egress_doors.egress_allowed(
        egress_doors.skill_door_id("iss_position", 0))
    assert allowed is True, why


def test_enable_after_a_file_is_tampered_with_is_refused(own_overlay, sandboxed):
    install_now()
    store.set_enabled("iss_position", False, reason="you asked me to")
    assert skill_flags() == {}
    folder = Path(store.skill_dir("iss_position"))
    (folder / "skill.py").write_text(
        (folder / "skill.py").read_text(encoding="utf-8") + "\n# tampered\n",
        encoding="utf-8")

    out = install.enable_installed("iss_position")
    assert out["ok"] is False and out["status"] == 409
    assert out["error"] == "needs_reapproval"
    assert "approved" in out["message"]
    assert store.read_state("iss_position")["enabled"] is False
    assert skill_flags() == {}, "a refused enable opened a door anyway"
    allowed, _why = egress_doors.egress_allowed(
        egress_doors.skill_door_id("iss_position", 0))
    assert allowed is False


def test_enable_with_a_door_digest_that_was_not_approved_is_refused(own_overlay, sandboxed):
    """The recorded digest is the reach the owner said yes to. A manifest that
    asks for a different one gets a consent card, not a switch."""
    install_now()
    store.set_enabled("iss_position", False, reason="you asked me to")
    record_path = Path(store.skill_dir("iss_position")) / "installed.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["door_flag"] = egress_doors.skill_door_flag(
        "iss_position", 0, APPROVED_HOSTS + ["evil.example.com"])
    record_path.write_text(json.dumps(record), encoding="utf-8")

    out = install.enable_installed("iss_position")
    assert out["ok"] is False and out["status"] == 409
    assert out["error"] == "needs_reapproval"
    assert "reach" in out["message"]
    assert store.read_state("iss_position")["enabled"] is False
    assert skill_flags() == {}


def test_enable_refuses_a_skill_that_is_not_installed():
    out = install.enable_installed("iss_position")
    assert out["ok"] is False and out["status"] == 404


def test_disabling_a_skill_clears_its_door_flag(own_overlay, sandboxed):
    install_now()
    assert skill_flags() == {approved_flag(): True}
    store.set_enabled("iss_position", False, reason="switched off")
    assert skill_flags() == {}
    allowed, _why = egress_doors.egress_allowed(
        egress_doors.skill_door_id("iss_position", 0))
    assert allowed is False, "a disabled skill's door was still open"


def test_removing_a_skill_clears_its_door_flag(own_overlay, sandboxed):
    install_now()
    assert skill_flags() == {approved_flag(): True}
    assert store.remove("iss_position") is True
    assert skill_flags() == {}, (
        "a removed skill left the owner's door flag behind, waiting for the "
        "next door of that name")


def test_reinstalling_after_a_removal_leaves_exactly_one_flag(own_overlay,
                                                              sandboxed):
    install_now()
    store.remove("iss_position")
    install.reset_for_tests()
    install_now()
    assert skill_flags() == {approved_flag(): True}
    assert len(skill_flags()) == 1


# ── the code view ───────────────────────────────────────────────────

def test_the_code_view_serves_the_installed_bytes_and_says_intact(sandboxed):
    import hashlib

    install_now()
    view = install.installed_bytes("iss_position")
    assert view["ok"] is True and view["installed"] is True
    assert view["intact"] is True and view["integrity_reason"] == ""
    assert [f["name"] for f in view["files"]] == ["manifest.json", "skill.py",
                                                  "tests_fixtures.json"]
    folder = Path(store.skill_dir("iss_position"))
    for entry in view["files"]:
        raw = (folder / entry["name"]).read_bytes()
        assert entry["text"].encode("utf-8") == raw, entry["name"]
        assert entry["bytes"] == len(raw)
        assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
        assert entry["sha256_recorded"] == entry["sha256"]
        assert entry["matches"] is True
    assert view["hosts"] == APPROVED_HOSTS
    assert view["domains"] == ["wheretheiss.at"]
    assert view["state"] == "installed" and view["enabled"] is True


def test_the_code_view_shows_the_drift_and_says_not_intact():
    install_now()
    folder = Path(store.skill_dir("iss_position"))
    (folder / "skill.py").write_text(
        (folder / "skill.py").read_text(encoding="utf-8") + "\n# tampered\n",
        encoding="utf-8")

    view = install.installed_bytes("iss_position")
    assert view["intact"] is False and view["integrity_reason"]
    entry = next(f for f in view["files"] if f["name"] == "skill.py")
    assert "# tampered" in entry["text"], "the view hid the change"
    assert entry["matches"] is False
    assert entry["sha256"] != entry["sha256_recorded"]
    # The other two files are untouched and say so.
    assert all(f["matches"] for f in view["files"] if f["name"] != "skill.py")


def test_the_code_view_refuses_what_is_not_an_installed_skill():
    with pytest.raises(Exception):
        install.installed_bytes("iss_position")      # nothing installed
    propose()                                         # a DRAFT is not installed
    with pytest.raises(Exception):
        install.installed_bytes("iss_position")
    with pytest.raises(Exception):
        install.installed_bytes("../../etc")
