"""What the skill validator REFUSES. Every test here is a closed door.

The interesting ones, in order of how much they matter:

* a trigger the appliance already answers: a skill
  must never be able to claim "turn off the lights", and the refusal is at
  INSTALL time so dispatch order is only the second defence;
* ``skill.py`` bigger than the consent card can honestly show;
* ``tests`` naming anything but the host's own harness;
* a symlink anywhere in the package.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from puffpai.skills import manifest as m                       # noqa: E402
from skillkit import load_example, make_draft                  # noqa: E402


@pytest.fixture
def base():
    manifest, _source, _fixtures = load_example()
    return manifest


def test_the_shipped_example_validates(base):
    clean = m.validate(base)
    assert clean["id"] == "iss_position"
    assert clean["permissions"]["network_hosts"] == ["api.wheretheiss.at"]
    assert clean["tests"] == ["tests.py"]


# ── trigger collision ─────────────────────────────────────────────

@pytest.mark.parametrize("stolen", [
    "turn off the lights",
    "what is the network status",
    "what's the weather",
    "what can you do",
    "is the front door locked",
    "what is in my vault",
])
def test_a_trigger_the_appliance_already_answers_is_refused(base, stolen):
    """A skill can never take a phrase the household already means something by."""
    with pytest.raises(m.SkillManifestError) as caught:
        m.validate(dict(base, triggers=[stolen]))
    assert "already answers" in str(caught.value)


def test_collision_check_consults_the_registry_ranker_too(base):
    """Not just declared phrases: anything ``find()`` claims is off limits.

    ``find`` is an eager ranker, and that is the point -- "where is the space
    station" scores against ``home.control``, so it is left to the appliance
    even though no capability declares it verbatim.
    """
    assert m.trigger_collision("where is the space station") is not None
    with pytest.raises(m.SkillManifestError):
        m.validate(dict(base, triggers=["where is the space station"]))


def test_a_skill_can_keep_its_own_triggers_when_revalidated(base):
    """Re-validating an INSTALLED skill must not find itself and refuse."""
    from puffpai import capability_registry as cr

    try:
        row = cr.Capability(
            "skill.iss_position", "skills", "ISS position", "(none)",
            "skill", cr.READ, phrases=("where is the iss",),
            dispatch_phrases=("where is the iss",), origin="skill:iss_position")
        cr.set_skill_overlay((row,))
        assert m.trigger_collision("where is the iss",
                                   skip_origin="skill:iss_position") is None
        assert m.trigger_collision("where is the iss") == "skill.iss_position"
    finally:
        cr.clear_skill_overlay()


def test_the_collision_check_fails_closed(base, monkeypatch):
    """A registry we cannot consult means every trigger collides."""
    import puffpai.capability_registry as cr

    def _boom(_query):
        raise RuntimeError("registry down")

    monkeypatch.setattr(cr, "find", _boom)
    assert m.trigger_collision("where is the iss") == "(registry unavailable)"


# ── shape refusals ──────────────────────────────────────────────────

@pytest.mark.parametrize("patch,needle", [
    ({"schema": "puffpai_app/v1"}, "schema"),
    ({"id": "No-Caps"}, "not usable"),
    ({"id": "x"}, "not usable"),
    ({"name": ""}, "no usable name"),
    ({"description": ""}, "no usable description"),
    ({"version": "one"}, "non-numeric version"),
    ({"author": "some-stranger"}, "author"),
    ({"triggers": []}, "no triggers"),
    ({"triggers": ["iss"]}, "one-word trigger"),
    ({"limits": {"max_requests": 99}}, "ceiling"),
    ({"limits": {"timeout_s": 600}}, "ceiling"),
    ({"tests": ["mine.py"]}, "test harness"),
    ({"tests": ["tests.py", "more.py"]}, "test harness"),
    ({"source_urls": ["http://plain.example.com"]}, "not https"),
])
def test_manifest_refusals(base, patch, needle):
    with pytest.raises(m.SkillManifestError) as caught:
        m.validate(dict(base, **patch))
    assert needle in str(caught.value)


@pytest.mark.parametrize("permissions,needle", [
    ({"network_hosts": ["8.8.8.8"], "files": [], "ha_domains": [], "secrets": []},
     "registrable NAME"),
    ({"network_hosts": ["localhost"], "files": [], "ha_domains": [], "secrets": []},
     "registrable NAME"),
    ({"network_hosts": ["*.example.com"], "files": [], "ha_domains": [],
      "secrets": []}, "registrable NAME"),
    ({"network_hosts": ["api.example.com:8443"], "files": [], "ha_domains": [],
      "secrets": []}, "registrable NAME"),
    ({"network_hosts": [], "files": ["/etc"], "ha_domains": [], "secrets": []},
     "only path"),
    ({"network_hosts": [], "files": [], "ha_domains": ["light"], "secrets": []},
     "Phase 1"),
    ({"network_hosts": [], "files": [], "ha_domains": [], "secrets": ["api_key"]},
     "Phase 1"),
])
def test_permission_refusals(base, permissions, needle):
    with pytest.raises(m.SkillManifestError) as caught:
        m.validate(dict(base, permissions=permissions))
    assert needle in str(caught.value)


def test_ha_and_secrets_are_refused_even_when_they_look_harmless(base):
    """Phase 1 has no vault tier and no HA tier; declaring one is a refusal,
    not a field that gets quietly ignored."""
    clean = m.validate(base)
    assert clean["permissions"]["ha_domains"] == []
    assert clean["permissions"]["secrets"] == []


# ── the bytes beside the manifest ───────────────────────────────────

def test_a_skill_bigger_than_the_card_is_refused(tmp_path):
    folder = make_draft(tmp_path / "big",
                        source="def run(inputs, ctx):\n    return {}\n"
                               + "# pad\n" * (m.MAX_SKILL_PY_LINES + 5))
    with pytest.raises(m.SkillManifestError) as caught:
        m.validate_dir(str(folder))
    assert "lines" in str(caught.value)


def test_a_skill_over_the_byte_ceiling_is_refused(tmp_path):
    folder = make_draft(tmp_path / "fat",
                        source="def run(inputs, ctx):\n    return {'x': '"
                               + "A" * (m.MAX_SKILL_PY_BYTES + 10) + "'}\n")
    with pytest.raises(m.SkillManifestError) as caught:
        m.validate_dir(str(folder))
    assert "bytes" in str(caught.value)


def test_a_package_with_no_run_entrypoint_is_refused(tmp_path):
    folder = make_draft(tmp_path / "norun", source="X = 1\n")
    with pytest.raises(m.SkillManifestError) as caught:
        m.validate_dir(str(folder))
    assert "run(inputs, ctx)" in str(caught.value)


def test_an_extra_file_is_refused(tmp_path):
    folder = make_draft(tmp_path / "extra")
    (Path(folder) / "helper.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(m.SkillManifestError) as caught:
        m.validate_dir(str(folder))
    assert "not part of a skill package" in str(caught.value)


def test_a_subdirectory_other_than_data_is_refused(tmp_path):
    folder = make_draft(tmp_path / "sub")
    os.makedirs(os.path.join(folder, "lib"))
    with pytest.raises(m.SkillManifestError) as caught:
        m.validate_dir(str(folder))
    assert "not an allowed directory" in str(caught.value)


def test_a_symlink_in_the_package_is_refused(tmp_path):
    """The card shows one file; a link would let the jail read another."""
    folder = Path(make_draft(tmp_path / "linked"))
    target = tmp_path / "elsewhere.py"
    target.write_text("def run(inputs, ctx):\n    return {}\n", encoding="utf-8")
    link = folder / "skill.py"
    link.unlink()
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("this box does not allow creating symlinks")
    with pytest.raises(m.SkillManifestError) as caught:
        m.validate_dir(str(folder))
    assert "symlink" in str(caught.value)


# ── fixtures are the test harness's input, and must assert something ──

def test_fixtures_with_no_cases_are_refused(tmp_path):
    folder = make_draft(tmp_path / "nocases",
                        fixtures={"responses": {"https://api.wheretheiss.at/v1/x":
                                                {"json": {}}}, "cases": []})
    with pytest.raises(m.SkillManifestError) as caught:
        m.validate_dir(str(folder))
    assert "no cases" in str(caught.value)


def test_fixtures_that_assert_nothing_are_refused(tmp_path):
    folder = make_draft(
        tmp_path / "empty",
        fixtures={"responses": {"https://api.wheretheiss.at/v1/x": {"json": {}}},
                  "cases": [{"inputs": {}, "expect": {"speech_contains": [],
                                                      "data": {}}}]})
    with pytest.raises(m.SkillManifestError) as caught:
        m.validate_dir(str(folder))
    assert "assert" in str(caught.value)


def test_fixtures_may_not_can_a_host_the_manifest_does_not_declare(tmp_path):
    _manifest, _source, fixtures = load_example()
    canned = dict(fixtures)
    canned["responses"] = dict(canned["responses"])
    canned["responses"]["https://evil.example.com/x"] = {"json": {}}
    folder = make_draft(tmp_path / "sneaky", fixtures=canned)
    with pytest.raises(m.SkillManifestError) as caught:
        m.validate_dir(str(folder))
    assert "does not declare" in str(caught.value)


def test_validate_dir_returns_a_sha_per_package_file(tmp_path):
    folder = make_draft(tmp_path / "hashed")
    _clean, shas = m.validate_dir(str(folder))
    assert set(shas) == set(m.PACKAGE_FILES)
    assert all(len(v) == 64 for v in shas.values())
    # The hashes are of the bytes ON DISK: touch a file and they move. This is
    # what makes "approval is of bytes" enforceable at install time.
    with open(os.path.join(folder, "skill.py"), "a", encoding="utf-8") as handle:
        handle.write("\n# a later edit\n")
    _again, shas2 = m.validate_dir(str(folder))
    assert shas2["skill.py"] != shas["skill.py"]
    assert shas2["manifest.json"] == shas["manifest.json"]
    assert json.loads(Path(folder, "manifest.json").read_text(
        encoding="utf-8"))["id"] == "iss_position"
