"""Submitting a skill of your own: the parser, the route, and the CLI twin.

Answers the owner's question -- "is it actually easy for people to attach their
programs and APIs?" -- with the thing that was missing: a way in for a skill
nobody at Cypher Sisters wrote. Before this, ``POST /skills/drafts/from-example``
could only stage something that shipped inside ``src/``, so a developer's own
package needed a shell on the appliance.

The test file is mostly refusals, because this is a parser for hostile input:
bytes off the network becoming files on disk beside code the owner will be asked
to approve. Every refusal asserts the same two things -- the request was refused
AND nothing was written -- since a half-written draft is a draft whose consent
card would describe files that are not the files on disk.

What is deliberately NOT claimed here: nothing in this file proves isolation.
The jail's assertions live in ``test_skills_sandbox.py`` behind ``needs_jail``.
This file proves that submitting cannot install, cannot open a door, and cannot
write outside ``_drafts/<id>/``.

Run: pytest tests/test_skills_submit.py -q

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from puffpai.security import consent_requests as creq          # noqa: E402
from puffpai.security import egress_doors                      # noqa: E402
from puffpai.skills import install, manifest, store, submit     # noqa: E402
import skillkit                                                # noqa: E402

SUBMITTED_ID = "tide_clock"


# ── a package a developer would plausibly write ──────────────────────

SKILL_PY = '''"""Read one number off a JSON API and say it."""

URL = "https://api.example.org/v1/tide"


def run(inputs, ctx):
    reading = ctx.fetch_json(URL)
    height = round(float(reading["height_m"]), 2)
    return {"speech": "The tide is {h} metres.".format(h=height),
            "data": {"height_m": height, "source": URL}}
'''

MANIFEST = {
    "schema": manifest.SCHEMA,
    "id": SUBMITTED_ID,
    "name": "Tide clock",
    "description": "The tide height from a public JSON API this household chose.",
    "version": 1,
    "author": "owner",
    # Both chosen because ``trigger_collision`` accepts them: the eager
    # ``capability_registry.find`` claims almost any household sentence, which
    # is the point of that rule and is worth a reader knowing.
    "triggers": ["tide height now", "how high is the tide"],
    "inputs": {"type": "object", "properties": {}, "required": []},
    "permissions": {"network_hosts": ["api.example.org"], "files": [],
                    "ha_domains": [], "secrets": []},
    "limits": {"timeout_s": 10, "max_requests": 1, "max_response_bytes": 8192},
    "tests": ["tests.py"],
    "source_urls": [],
}

FIXTURES = {
    "responses": {"https://api.example.org/v1/tide": {"json": {"height_m": 1.234}}},
    "cases": [{"inputs": {},
               "expect": {"speech_contains": ["The tide is 1.23 metres."],
                          "data": {"height_m": 1.23}}}],
}


def package_files(*, manifest_patch=None, source=None, drop=()) -> dict:
    """``{name: bytes}`` for a valid submission, with per-test overrides."""
    doc = dict(MANIFEST)
    if manifest_patch:
        doc.update(manifest_patch)
    files = {
        "manifest.json": json.dumps(doc, indent=2).encode("utf-8"),
        "skill.py": (source if source is not None else SKILL_PY).encode("utf-8"),
        "tests_fixtures.json": json.dumps(FIXTURES, indent=2).encode("utf-8"),
    }
    for name in drop:
        files.pop(name, None)
    return files


def zip_of(files: dict, *, extra=(), arcname_map=None) -> bytes:
    """A zip holding *files*, plus raw ``(name, bytes, mode)`` members."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, blob in files.items():
            archive.writestr((arcname_map or {}).get(name, name), blob)
        for name, blob, mode in extra:
            info = zipfile.ZipInfo(name)
            if mode:
                info.external_attr = mode << 16
            archive.writestr(info, blob)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Own data dir, own skills tree, own consent queue. No sandbox faked."""
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


def drafts_tree() -> list:
    """Every path under the drafts root, relative, sorted. The blast radius."""
    root = Path(store.drafts_root())
    return sorted(str(p.relative_to(root)).replace("\\", "/")
                  for p in root.rglob("*"))


def assert_nothing_written():
    assert drafts_tree() == [], f"a refusal left files behind: {drafts_tree()}"
    assert store.installed_ids() == []


# ===========================================================================
# 1. the happy path: submit -> draft -> the same consent card
# ===========================================================================

class TestSubmitAccepts:

    def test_a_json_submission_becomes_a_draft_and_one_consent_request(self):
        result = submit.submit_and_propose(SUBMITTED_ID, package_files())
        assert result["submitted"] is True
        assert result["proposed"] is True and result["awaiting_approval"] is True
        assert result["id"] == SUBMITTED_ID

        # It is a DRAFT, not an install, and no door exists for it yet.
        assert store.installed_ids() == []
        assert sorted(drafts_tree()) == [
            SUBMITTED_ID, f"{SUBMITTED_ID}/manifest.json",
            f"{SUBMITTED_ID}/skill.py",
            f"{SUBMITTED_ID}/tests_fixtures.json"]
        assert egress_doors.door(egress_doors.skill_door_id(SUBMITTED_ID, 0)) is None

        # The card is the SAME card an example gets: bytes, hashes, hosts.
        card = install.draft_bytes(SUBMITTED_ID)
        assert card["hosts"] == ["api.example.org"]
        assert card["domains"] == ["example.org"]
        assert {f["name"] for f in card["files"]} == set(manifest.PACKAGE_FILES)
        code = next(f for f in card["files"] if f["name"] == "skill.py")
        assert code["text"] == SKILL_PY and len(code["sha256"]) == 64

        [request] = [r for r in creq._load() if r["action"] == "skill_install"]
        assert request["context"]["skill_id"] == SUBMITTED_ID
        assert request["context"]["sha256"]["skill.py"] == code["sha256"]
        assert request["status"] == "open", "a submission resolved its own card"

    def test_a_zip_submission_lands_the_identical_bytes(self):
        files = package_files()
        result = submit.submit_and_propose(
            SUBMITTED_ID, submit.collect_from_zip(zip_of(files)))
        assert result["proposed"] is True
        folder = Path(store.draft_dir(SUBMITTED_ID))
        for name, blob in files.items():
            assert (folder / name).read_bytes() == blob, name

    def test_the_id_can_come_from_the_manifest_alone(self):
        files = package_files()
        assert submit.manifest_id_of(files) == SUBMITTED_ID

    def test_a_submitted_skill_reaches_only_what_it_declared(self):
        """The broker is host-set-first, and the set is the manifest's."""
        from puffpai.skills import broker as _broker

        submit.submit(SUBMITTED_ID, package_files())
        card = install.draft_bytes(SUBMITTED_ID)
        b = _broker.LiveBroker(SUBMITTED_ID, card["hosts"],
                               card["manifest"]["limits"])
        assert b.check("https://api.example.org/v1/tide") == "api.example.org"
        for url in ("https://evil.example.com/x",
                    "http://api.example.org/v1/tide",
                    "https://api.example.org:8443/v1/tide",
                    "https://user:pw@api.example.org/v1/tide"):
            with pytest.raises(_broker.BrokerRefusal):
                b.check(url)


# ===========================================================================
# 2. ids
# ===========================================================================

class TestIds:

    @pytest.mark.parametrize("bad", [
        "", "a", "A_Skill", "has-dash", "has space", "héllo", "x" * 41,
        "../escape", "9leading_digit_ok_for_submit_re_not_manifest",
        "..", ".", "_leading",
    ])
    def test_an_unusable_id_is_refused_and_writes_nothing(self, bad):
        with pytest.raises(submit.SubmissionRefused):
            submit.submit(bad, package_files())
        assert_nothing_written()

    def test_the_manifest_id_and_the_submitted_id_must_agree(self):
        with pytest.raises(submit.SubmissionRefused) as exc:
            submit.submit("other_name", package_files())
        assert "agree" in str(exc.value)
        assert drafts_tree() == [], "a mismatched id left a draft behind"

    def test_a_second_submission_of_the_same_draft_needs_replace(self):
        submit.submit(SUBMITTED_ID, package_files())
        with pytest.raises(submit.SubmissionRefused) as exc:
            submit.submit(SUBMITTED_ID, package_files())
        assert "replace" in str(exc.value)

        changed = package_files(manifest_patch={"version": 2})
        out = submit.submit(SUBMITTED_ID, changed, replace=True)
        assert out["manifest"]["version"] == 2

    def test_an_installed_id_is_refused_even_with_replace(self, monkeypatch):
        os.makedirs(store.skill_dir(SUBMITTED_ID), exist_ok=True)
        for replace in (False, True):
            with pytest.raises(submit.SubmissionRefused) as exc:
                submit.submit(SUBMITTED_ID, package_files(), replace=replace)
            assert "already installed" in str(exc.value)
        assert drafts_tree() == []


# ===========================================================================
# 3. zip-slip, symlinks, nesting, and the ceilings
# ===========================================================================

class TestZipSlip:

    @pytest.mark.parametrize("evil", [
        "../escape.py",
        "../../etc/cron.d/x.txt",
        "a/../../b.py",
        "/etc/passwd.txt",
        "//host/share/x.py",
        "C:/Windows/System32/x.py",
        "c:x.py",
        "..\\escape.py",
        "sub\\dir\\x.py",
        "./x.py",
        "__pycache__/skill.py",
        "sub/__pycache__/x.py",
        ".git/config.txt",
        "x" * 200 + ".py",
    ])
    def test_a_traversing_or_odd_member_name_is_refused(self, evil):
        raw = zip_of(package_files(), extra=[(evil, b"x = 1\n", 0o100644)])
        with pytest.raises(submit.SubmissionRefused):
            submit.collect_from_zip(raw)
        assert_nothing_written()

    def test_a_symlink_member_is_refused(self):
        raw = zip_of(package_files(),
                     extra=[("notes.txt", b"/etc/shadow", 0o120777)])
        with pytest.raises(submit.SubmissionRefused) as exc:
            submit.collect_from_zip(raw)
        assert "symlink" in str(exc.value)
        assert_nothing_written()

    def test_a_directory_shaped_member_with_a_mode_is_refused(self):
        raw = zip_of(package_files(), extra=[("data.txt", b"", 0o040755)])
        with pytest.raises(submit.SubmissionRefused) as exc:
            submit.collect_from_zip(raw)
        assert "regular file" in str(exc.value)

    @pytest.mark.parametrize("name", ["inner.zip", "bundle.tar", "x.tar.gz",
                                      "skill.pyc", "run.sh", "Makefile",
                                      "lib.so"])
    def test_a_file_kind_a_package_may_not_carry_is_refused(self, name):
        raw = zip_of(package_files(), extra=[(name, b"data", 0o100644)])
        with pytest.raises(submit.SubmissionRefused):
            submit.collect_from_zip(raw)
        assert_nothing_written()

    def test_an_archive_wearing_a_text_files_name_is_refused(self):
        inner = zip_of(package_files())
        files = package_files()
        files["notes.txt"] = inner
        with pytest.raises(submit.SubmissionRefused) as exc:
            submit.collect_from_zip(zip_of(files))
        assert "archive" in str(exc.value)
        assert_nothing_written()

    def test_a_tar_wearing_a_text_files_name_is_refused(self):
        import tarfile

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            info = tarfile.TarInfo("inner.txt")
            info.size = 3
            tar.addfile(info, io.BytesIO(b"abc"))
        files = package_files()
        files["notes.txt"] = buffer.getvalue()
        with pytest.raises(submit.SubmissionRefused) as exc:
            submit.collect_from_zip(zip_of(files))
        assert "tar archive" in str(exc.value)

    def test_an_encrypted_zip_is_refused(self):
        """``zipfile`` cannot WRITE an encrypted member, so the bit is set in
        the bytes: general-purpose flags live at +6 in each local file header
        (``PK\\x03\\x04``) and at +8 in each central directory entry
        (``PK\\x01\\x02``)."""
        raw = bytearray(zip_of(package_files()))
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            at = raw.find(signature)
            while at != -1:
                raw[at + offset] |= 0x01
                at = raw.find(signature, at + 1)
        with pytest.raises(submit.SubmissionRefused) as exc:
            submit.collect_from_zip(bytes(raw))
        assert "encrypted" in str(exc.value)

    def test_bytes_that_are_not_a_zip_are_refused(self):
        with pytest.raises(submit.SubmissionRefused):
            submit.collect_from_zip(b"not a zip at all")
        with pytest.raises(submit.SubmissionRefused):
            submit.collect_from_zip(b"")


class TestCeilings:

    def test_too_many_files_is_refused_by_counting(self):
        extra = [(f"pad{i}.txt", b"x", 0o100644)
                 for i in range(submit.MAX_FILES + 1)]
        with pytest.raises(submit.SubmissionRefused) as exc:
            submit.collect_from_zip(zip_of(package_files(), extra=extra))
        assert "at most" in str(exc.value)
        assert_nothing_written()

    def test_a_declared_size_over_the_cap_is_refused(self):
        big = package_files()
        big["skill.py"] = SKILL_PY.encode("utf-8") + b"\n# " + b"p" * (
            submit.MAX_TOTAL_BYTES + 10)
        with pytest.raises(submit.SubmissionRefused) as exc:
            submit.collect_from_zip(zip_of(big))
        assert "KB" in str(exc.value)
        assert_nothing_written()

    def test_a_zip_bomb_whose_header_lies_is_refused_while_reading(self):
        """The cap is enforced on the bytes actually read, not on the header.

        A ZIP64 central-directory entry can claim any ``file_size`` it likes, so
        the declared-size check above is the cheap arm and this is the real one:
        the member is read with a limit and refused when it overruns.
        """
        payload = b"A" * (submit.MAX_TOTAL_BYTES + 4096)
        raw = bytearray(zip_of(package_files(),
                              extra=[("pad.txt", payload, 0o100644)]))
        # Rewrite the declared uncompressed size to a small lie. Both copies
        # (local header and central directory) carry it; patching every 4-byte
        # occurrence of the true value covers both without parsing the format.
        true_size = len(payload).to_bytes(4, "little")
        raw = bytes(raw).replace(true_size, (16).to_bytes(4, "little"))
        with pytest.raises(submit.SubmissionRefused) as exc:
            submit.collect_from_zip(raw)
        assert "KB" in str(exc.value) or "at most" in str(exc.value)
        assert_nothing_written()

    def test_an_oversized_upload_is_refused_before_it_is_parsed(self):
        with pytest.raises(submit.SubmissionRefused) as exc:
            submit.collect_from_zip(b"P" * (submit.MAX_UPLOAD_BYTES + 1))
        assert "larger than" in str(exc.value)


class TestLayout:

    @pytest.mark.parametrize("missing", ["manifest.json", "skill.py",
                                         "tests_fixtures.json"])
    def test_an_incomplete_package_is_refused(self, missing):
        with pytest.raises(submit.SubmissionRefused) as exc:
            submit.collect_from_mapping(
                {k: v.decode() for k, v in
                 package_files(drop=[missing]).items()})
        assert missing in str(exc.value)
        assert_nothing_written()

    def test_a_data_subdirectory_is_refused_on_submission(self):
        """Bytes the consent card does not show are bytes nobody approved."""
        raw = zip_of(package_files(),
                     extra=[("data/seed.json", b"{}", 0o100644)])
        with pytest.raises(submit.SubmissionRefused) as exc:
            submit.collect_from_zip(raw)
        assert "consent card" in str(exc.value)
        assert_nothing_written()

    def test_a_readme_is_refused_rather_than_silently_dropped(self):
        raw = zip_of(package_files(),
                     extra=[("README.md", b"# hi\n", 0o100644)])
        with pytest.raises(submit.SubmissionRefused) as exc:
            submit.collect_from_zip(raw)
        assert "README.md" in str(exc.value)

    def test_bytes_that_are_not_utf8_are_refused(self):
        files = package_files()
        files["skill.py"] = b"def run(inputs, ctx):\n    return {}  # \xff\xfe\n"
        with pytest.raises(submit.SubmissionRefused) as exc:
            submit.check_layout(files)
        assert "UTF-8" in str(exc.value)


# ===========================================================================
# 4. the manifest's own rules still apply, and a refusal writes nothing
# ===========================================================================

class TestManifestRulesStillApply:

    def test_a_trigger_the_appliance_already_answers_is_refused(self):
        with pytest.raises(manifest.SkillManifestError) as exc:
            submit.submit(SUBMITTED_ID, package_files(
                manifest_patch={"triggers": ["turn off the lights"]}))
        assert "already answers" in str(exc.value)
        assert_nothing_written()

    def test_an_ip_literal_host_is_refused(self):
        for host in ("192.168.1.50", "127.0.0.1", "8.8.8.8", "localhost",
                     "*.example.org", "https://api.example.org"):
            with pytest.raises(manifest.SkillManifestError):
                submit.submit(SUBMITTED_ID, package_files(manifest_patch={
                    "permissions": {"network_hosts": [host], "files": [],
                                    "ha_domains": [], "secrets": []}}))
            assert_nothing_written()

    def test_asking_for_home_assistant_or_the_vault_is_refused(self):
        for key in ("ha_domains", "secrets"):
            perms = {"network_hosts": ["api.example.org"], "files": [],
                     "ha_domains": [], "secrets": []}
            perms[key] = ["anything"]
            with pytest.raises(manifest.SkillManifestError):
                submit.submit(SUBMITTED_ID,
                              package_files(manifest_patch={"permissions": perms}))
            assert_nothing_written()

    def test_a_third_party_author_is_refused(self):
        with pytest.raises(manifest.SkillManifestError) as exc:
            submit.submit(SUBMITTED_ID,
                          package_files(manifest_patch={"author": "acme"}))
        assert "signature" in str(exc.value)
        assert_nothing_written()

    def test_a_package_with_no_run_entrypoint_is_refused(self):
        with pytest.raises(manifest.SkillManifestError) as exc:
            submit.submit(SUBMITTED_ID,
                          package_files(source="X = 1\n"))
        assert "run(inputs, ctx)" in str(exc.value)
        assert_nothing_written()

    def test_fixtures_that_cannot_fail_are_refused(self):
        files = package_files()
        files["tests_fixtures.json"] = json.dumps({
            "responses": {"https://api.example.org/v1/tide": {"json": {}}},
            "cases": [{"inputs": {}, "expect": {}}]}).encode("utf-8")
        with pytest.raises(manifest.SkillManifestError):
            submit.submit(SUBMITTED_ID, files)
        assert_nothing_written()

    def test_nothing_in_the_submitted_code_is_executed(self):
        """A skill that would explode on import still only gets validated.

        ``validate_dir`` reaches ``lint.has_run_entrypoint``, which parses the
        source with ``ast`` -- there is no import and no exec anywhere on the
        submit path, so a package whose module body raises is accepted as a
        draft and fails later, in the jail, where failing is safe.
        """
        source = ('raise SystemExit("module body ran")\n\n\n'
                  'def run(inputs, ctx):\n'
                  '    return {"speech": "hi", "data": {}}\n')
        out = submit.submit(SUBMITTED_ID, package_files(source=source))
        assert out["ok"] is True


# ===========================================================================
# 5. the HTTP route
# ===========================================================================

@pytest.fixture()
def client(isolated):
    from puffpai import api_server as mod
    mod.app.config["TESTING"] = True
    mod._rate_limit_store.clear()
    with mod.app.test_client() as c:
        yield c, {"X-API-Key": mod._API_KEY}


class TestRoute:

    def test_it_needs_the_api_key(self, client):
        c, _ = client
        resp = c.post('/skills/drafts', json={"files": {}})
        assert resp.status_code == 401
        assert store.installed_ids() == []

    def test_it_is_not_exempt_for_a_same_host_caller(self):
        """Submitting code is never on the loopback exemption list."""
        from puffpai.security import endpoint_gate

        assert 'skills_draft_submit' not in endpoint_gate.LOCAL_ONLY_ENDPOINTS
        assert endpoint_gate.local_caller_allowed(
            'skills_draft_submit', '127.0.0.1') is False

    def test_a_json_body_submits_and_proposes(self, client):
        c, headers = client
        body = {"id": SUBMITTED_ID,
                "files": {k: v.decode("utf-8")
                          for k, v in package_files().items()}}
        resp = c.post('/skills/drafts', json=body, headers=headers)
        assert resp.status_code == 200, resp.get_json()
        payload = resp.get_json()
        assert payload["submitted"] is True and payload["installed"] is False
        assert payload["awaiting_approval"] is True
        assert payload["hosts"] == ["api.example.org"]
        assert store.installed_ids() == []
        assert c.get(f'/skills/{SUBMITTED_ID}/draft',
                     headers=headers).get_json()["ok"] is True

    def test_a_zip_upload_submits_and_proposes(self, client):
        c, headers = client
        data = {"file": (io.BytesIO(zip_of(package_files())), "skill.zip")}
        resp = c.post('/skills/drafts', data=data, headers=headers,
                      content_type='multipart/form-data')
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["id"] == SUBMITTED_ID
        assert store.installed_ids() == []

    def test_a_refused_package_answers_422_and_writes_nothing(self, client):
        c, headers = client
        data = {"file": (io.BytesIO(zip_of(
            package_files(), extra=[("../escape.py", b"x", 0o100644)])),
            "skill.zip")}
        resp = c.post('/skills/drafts', data=data, headers=headers,
                      content_type='multipart/form-data')
        assert resp.status_code == 422
        assert resp.get_json()["submitted"] is False
        assert_nothing_written()

    def test_an_empty_body_is_a_400_with_instructions(self, client):
        c, headers = client
        resp = c.post('/skills/drafts', json={}, headers=headers)
        assert resp.status_code == 400
        assert 'manifest.json' in resp.get_json()["error"]

    def test_replace_only_covers_a_draft(self, client):
        c, headers = client
        body = {"files": {k: v.decode("utf-8")
                          for k, v in package_files().items()}}
        assert c.post('/skills/drafts', json=body,
                      headers=headers).status_code == 200
        assert c.post('/skills/drafts', json=body,
                      headers=headers).status_code == 422
        assert c.post('/skills/drafts?replace=1', json=body,
                      headers=headers).status_code == 200

    def test_the_route_cannot_install_and_cannot_open_a_door(self, client):
        c, headers = client
        body = {"files": {k: v.decode("utf-8")
                          for k, v in package_files().items()}}
        c.post('/skills/drafts', json=body, headers=headers)
        assert store.installed_ids() == []
        door_id = egress_doors.skill_door_id(SUBMITTED_ID, 0)
        assert egress_doors.door(door_id) is None
        allowed, _reason = egress_doors.egress_allowed(door_id)
        assert allowed is False


# ===========================================================================
# 6. the full loop for a SUBMITTED skill, with a stand-in for the jail
# ===========================================================================

def test_a_submitted_skill_gets_its_own_door_when_the_owner_approves(
        monkeypatch, tmp_path):
    """The door a submitted skill gets is built from ITS manifest, shut by
    default, and keyed to the host set that was on the card."""
    from puffpai.ha_bridge import HUMAN_CONFIRMED
    from puffpai.skills import sandbox

    egress_doors.set_overlay_override(
        str(tmp_path / "own" / "egress_doors.json"))
    egress_doors.reset_cache()
    monkeypatch.setattr(sandbox, "sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox, "execute", skillkit.fake_execute)
    try:
        submitted = submit.submit_and_propose(SUBMITTED_ID, package_files())
        assert submitted["proposed"] is True

        door_id = egress_doors.skill_door_id(SUBMITTED_ID, 0)
        assert egress_doors.door(door_id) is None, "a draft registered a door"

        resolved = creq.resolve(f"skill:{SUBMITTED_ID}", approved=True,
                                by="user_via_ui")
        out = install.apply_approved_install(request=resolved,
                                             human_confirmed=HUMAN_CONFIRMED)
        assert out["ok"] and out["test_ok"] and out["door_opened"], out

        door = egress_doors.door(door_id)
        assert door is not None
        assert door.hosts == ("api.example.org",)
        assert door.default is False, "a skill door shipped open"
        assert egress_doors.skill_of(door) == SUBMITTED_ID
        assert door in egress_doors.all_doors()

        # The flag written is the digest of the APPROVED host set, and only it.
        expected = egress_doors.skill_door_flag(SUBMITTED_ID, 0,
                                               ["api.example.org"])
        overlay = json.loads(
            Path(egress_doors.overlay_path()).read_text(encoding="utf-8"))
        flags = {k: v for k, v in overlay["doors"].items()
                 if k.startswith("skill_")}
        assert flags == {expected: True}, flags
        assert egress_doors.egress_allowed(door_id)[0] is True

        # A wider host set is a different key, and nobody opened it.
        wider = egress_doors.skill_door_flag(
            SUBMITTED_ID, 0, ["api.example.org", "evil.example.com"])
        assert wider != expected and wider not in overlay["doors"]

        # Switch it off: the door and the owner's yes go together.
        store.set_enabled(SUBMITTED_ID, False, reason="you asked me to")
        overlay = json.loads(
            Path(egress_doors.overlay_path()).read_text(encoding="utf-8"))
        assert not [k for k in overlay["doors"] if k.startswith("skill_")]
        assert egress_doors.egress_allowed(door_id)[0] is False
    finally:
        egress_doors.set_overlay_override(None)
        egress_doors.reset_cache()


def test_the_generated_egress_doc_renders_a_submitted_skills_door(monkeypatch):
    """A submitted skill's hosts appear in the published egress document."""
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "gen_egress_doc_submit", root / "scripts" / "gen_egress_doc.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # The rule text is there with no skills installed...
    plain = module.render()
    assert "## Skills you install" in plain
    assert "permissions.network_hosts" in plain
    assert "No skill is installed on the box" in plain

    # ...and a registered skill door is rendered by name and by host.
    egress_doors.register_skill_doors([(SUBMITTED_ID, [{
        "hosts": ["api.example.org"],
        "purpose": MANIFEST["description"],
        "data_sent_plain": "The web address this skill asks for."}])])
    try:
        rendered = module.render()
        assert f"### `skill:{SUBMITTED_ID}:0`" in rendered
        assert "api.example.org" in rendered
        assert "**Ships:** shut" in rendered
    finally:
        egress_doors.clear_skill_doors()


# ===========================================================================
# 7. the CLI twin
# ===========================================================================

class TestCli:

    def test_submit_from_a_directory_then_list_and_status(self, tmp_path, capsys):
        from puffpai import cli

        package = tmp_path / "tide_clock"
        package.mkdir()
        for name, blob in package_files().items():
            (package / name).write_bytes(blob)

        assert cli.main(["skills", "submit", str(package)]) == 0
        out = capsys.readouterr().out
        assert f"Submitted: {SUBMITTED_ID}" in out
        assert "api.example.org" in out
        assert "approve" in out.lower()
        assert store.installed_ids() == []

        assert cli.main(["skills", "list"]) == 0
        listed = capsys.readouterr().out
        assert "No skills installed." in listed
        assert SUBMITTED_ID in listed          # as a waiting draft

        assert cli.main(["skills", "status", SUBMITTED_ID]) == 0
        assert "waiting for your approval" in capsys.readouterr().out

    def test_submit_from_a_zip(self, tmp_path, capsys):
        from puffpai import cli

        archive = tmp_path / "tide.zip"
        archive.write_bytes(zip_of(package_files()))
        assert cli.main(["skills", "submit", str(archive)]) == 0
        assert f"Submitted: {SUBMITTED_ID}" in capsys.readouterr().out

    def test_a_refused_package_says_nothing_was_written(self, tmp_path, capsys):
        from puffpai import cli

        archive = tmp_path / "evil.zip"
        archive.write_bytes(zip_of(package_files(),
                                   extra=[("../escape.py", b"x", 0o100644)]))
        assert cli.main(["skills", "submit", str(archive)]) == 1
        assert "Nothing was written" in capsys.readouterr().out
        assert_nothing_written()

    def test_a_missing_path_is_refused(self, tmp_path, capsys):
        from puffpai import cli

        assert cli.main(["skills", "submit", str(tmp_path / "nope")]) == 1
        assert "not a folder or a zip" in capsys.readouterr().out

    def test_status_of_an_unknown_skill(self, capsys):
        from puffpai import cli

        assert cli.main(["skills", "status", "no_such_skill"]) == 1
        assert "No skill or draft" in capsys.readouterr().out


class TestBodySizeGuards:
    """The two arms cap themselves differently, and both are asserted.

    Multipart caps itself with a bounded read; JSON cannot, because Flask
    buffers the whole body before ``get_json`` is reachable. A chunked JSON
    body has no declared length to check, so it is refused rather than read.
    """

    def test_a_declared_length_over_the_cap_is_413(self, client):
        from puffpai import api_server as mod

        c, headers = client
        resp = c.post('/skills/drafts', headers=dict(
            headers, **{'Content-Length': str(mod._SKILL_SUBMIT_MAX_BYTES + 1),
                        'Content-Type': 'application/json'}),
            data=b'{}' + b' ' * mod._SKILL_SUBMIT_MAX_BYTES)
        assert resp.status_code == 413
        assert_nothing_written()

    def test_a_chunked_json_body_is_411_not_read(self, client):
        c, headers = client
        resp = c.post('/skills/drafts',
                      headers=dict(headers, **{
                          'Content-Type': 'application/json'}),
                      input_stream=io.BytesIO(b'{"files": {}}'),
                      # What a chunked request looks like to WSGI: a body with
                      # no declared length, terminated by the server.
                      environ_overrides={'HTTP_TRANSFER_ENCODING': 'chunked',
                                         'CONTENT_LENGTH': None})
        assert resp.status_code == 411
        assert 'Content-Length' in resp.get_json()["error"]
        assert_nothing_written()

    def test_an_oversized_zip_upload_is_413(self, client):
        from puffpai import api_server as mod

        c, headers = client
        blob = b'PK\x03\x04' + b'x' * mod._SKILL_SUBMIT_MAX_BYTES
        resp = c.post('/skills/drafts',
                      data={"file": (io.BytesIO(blob), "big.zip")},
                      headers=headers, content_type='multipart/form-data')
        assert resp.status_code == 413
        assert_nothing_written()
