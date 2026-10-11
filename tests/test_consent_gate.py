"""Unit tests for security.consent_gate.

Pure-stdlib; no HA, no network. Run: pytest tests/test_consent_gate.py -q
"""
import builtins
import importlib
import json
import os
import sys

import pytest


def _load(tmp_path, monkeypatch, enforce="0"):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUFFPAI_ENFORCE_CONSENT", enforce)
    import puffpai.security.consent_gate as cg
    importlib.reload(cg)
    cg._store = None
    cg.ConsentStore._legacy_cache = None
    return cg


def _states(*eids):
    return [{"entity_id": e} for e in eids]


VALID = {
    "name": "Night lights off",
    "trigger_type": "time",
    "trigger_value": "23:00",
    "trigger_entity_domain": "time",
    "trigger_entity_hint": "",
    "conditions": [],
    "action_type": "turn_off",
    "action_entity_domain": "light",
    "action_entity_hint": "light.*living*",
    "action_details": "",
    "description": "turn off living room lights at 11pm",
}


def test_schema_valid(tmp_path, monkeypatch):
    cg = _load(tmp_path, monkeypatch)
    ok, norm, errs = cg.validate_automation(VALID)
    assert ok and not errs and norm["name"] == "Night lights off"


def test_schema_rejects_missing_and_bad_enum(tmp_path, monkeypatch):
    cg = _load(tmp_path, monkeypatch)
    bad = dict(VALID, trigger_type="webhook", action_type="explode")
    bad.pop("name")
    ok, _, errs = cg.validate_automation(bad)
    assert not ok and len(errs) >= 3


def test_schema_rejects_non_dict(tmp_path, monkeypatch):
    cg = _load(tmp_path, monkeypatch)
    ok, _, errs = cg.validate_automation(["not", "a", "dict"])
    assert not ok


def test_consent_default_pending_and_approve(tmp_path, monkeypatch):
    cg = _load(tmp_path, monkeypatch)
    st = cg.get_store()
    assert st.status("light.kitchen") == "pending"
    st.approve("light.kitchen", by="test", zone="zone3")
    assert st.status("light.kitchen") == "approved"
    # persisted
    data = json.load(open(os.path.join(str(tmp_path), "device_consent.json")))
    assert data["light.kitchen"]["zone3_applied"] is True


def test_review_denies_unconsented_target(tmp_path, monkeypatch):
    cg = _load(tmp_path, monkeypatch)
    v = cg.sentinel_review(VALID, _states("light.living_room", "light.kitchen"))
    assert v["action"] == "deny"
    assert v["binding"] is False  # dry-run default
    assert v["targets"] == ["light.living_room"]


def test_review_allows_consented_target(tmp_path, monkeypatch):
    cg = _load(tmp_path, monkeypatch)
    cg.get_store().approve("light.living_room")
    v = cg.sentinel_review(VALID, _states("light.living_room"))
    assert v["action"] == "allow" and v["reasons"] == []


def test_review_binding_when_enforced(tmp_path, monkeypatch):
    cg = _load(tmp_path, monkeypatch, enforce="1")
    v = cg.sentinel_review(VALID, _states("light.living_room"))
    assert v["action"] == "deny" and v["binding"] is True


def test_review_denies_hallucinated_glob(tmp_path, monkeypatch):
    cg = _load(tmp_path, monkeypatch)
    auto = dict(VALID, action_entity_hint="light.*basement*")
    v = cg.sentinel_review(auto, _states("light.living_room"))
    assert v["action"] == "deny"
    assert any("matches no known entity" in r for r in v["reasons"])


def test_review_denies_safety_critical_without_consent(tmp_path, monkeypatch):
    cg = _load(tmp_path, monkeypatch)
    auto = dict(VALID, action_type="lock", action_entity_domain="lock",
                action_entity_hint="lock.front_door")
    v = cg.sentinel_review(auto, _states("lock.front_door"))
    assert v["action"] == "deny"


def test_review_denies_external_egress(tmp_path, monkeypatch):
    cg = _load(tmp_path, monkeypatch)
    cg.get_store().approve("light.living_room")
    auto = dict(VALID, action_details="POST to https://evil.example/hook")
    v = cg.sentinel_review(auto, _states("light.living_room"))
    assert v["action"] == "deny"
    assert any("egress" in r for r in v["reasons"])


def test_review_blast_radius(tmp_path, monkeypatch):
    cg = _load(tmp_path, monkeypatch)
    eids = [f"light.zone_{i}" for i in range(12)]
    for e in eids:
        cg.get_store().approve(e)
    auto = dict(VALID, action_entity_hint="light.zone_*")
    v = cg.sentinel_review(auto, _states(*eids))
    assert v["action"] == "deny"
    assert any("blast radius" in r for r in v["reasons"])


def test_legacy_blanket_flags_not_trusts(tmp_path, monkeypatch):
    legacy = tmp_path / "ha_device_approvals.jsonl"
    legacy.write_text(json.dumps({"entity_id": "light.living_room"}) + "\n")
    cg = _load(tmp_path, monkeypatch)
    assert cg.get_store().status("light.living_room") == "legacy_blanket"
    v = cg.sentinel_review(VALID, _states("light.living_room"))
    assert v["action"] == "flag"  # flagged, not denied, not silently allowed
    assert any("blanket" in r for r in v["reasons"])


def test_review_audit_trail_written(tmp_path, monkeypatch):
    cg = _load(tmp_path, monkeypatch)
    cg.sentinel_review(VALID, _states("light.living_room"))
    lines = open(os.path.join(str(tmp_path), "sentinel_reviews.jsonl")).readlines()
    assert len(lines) == 1 and json.loads(lines[0])["action"] == "deny"


# ── Unwritable audit trail is loud, not silent ──────────────────────────
#
# A bare `except OSError: pass` around the append would let a box whose
# review log has become unwritable keep gating with NO
# record of what it allowed and say nothing. Reachable in production: the
# file-level adoption in _state_file() can resolve the log into the read-only
# code tree. The verdict path stays unaffected -- the log must not raise -- but
# the loss is now reported once per resolved path.


def _blocked_log(tmp_path):
    """A review-log path whose parent directory is a regular FILE.

    Makes the append fail for real (``os.makedirs`` re-raises FileExistsError
    when the name exists and is not a directory) instead of monkeypatching
    stdlib out from under the module.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    return blocker / "sentinel_reviews.jsonl"


def _gate_errors(caplog, cg):
    return [r for r in caplog.records
            if r.levelname == "ERROR" and r.name == cg.__name__]


def test_unwritable_review_log_logs_once_and_keeps_the_verdict(
        tmp_path, monkeypatch, caplog):
    """The gate still answers; the loss of its audit trail is reported once."""
    cg = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(cg, "REVIEW_LOG", str(_blocked_log(tmp_path)))

    with caplog.at_level("ERROR", logger=cg.__name__):
        first = cg.sentinel_review(VALID, _states("light.living_room"))
        second = cg.sentinel_review(VALID, _states("light.living_room"))

    # (a) the verdict path is untouched -- same action, same keys as ever
    assert first["action"] == "deny" and first["binding"] is False
    assert first["targets"] == ["light.living_room"]
    assert second["action"] == "deny"
    assert set(first) == {"action", "binding", "reasons", "targets",
                          "reviewed_at", "task_text", "automation_name"}

    # (b) rate-limited: one line for two consecutive calls on the same path
    errors = _gate_errors(caplog, cg)
    assert len(errors) == 1
    msg = errors[0].getMessage()
    assert str(_blocked_log(tmp_path)) in msg
    assert "NOT being recorded" in msg


def test_unwritable_review_log_reports_each_distinct_path(
        tmp_path, monkeypatch, caplog):
    """Rate-limiting keys on the path: a relocation deserves a fresh line."""
    cg = _load(tmp_path, monkeypatch)

    with caplog.at_level("ERROR", logger=cg.__name__):
        for sub in ("one", "two"):
            monkeypatch.setattr(
                cg, "REVIEW_LOG", str(_blocked_log(tmp_path / sub)))
            cg.sentinel_review(VALID, _states("light.living_room"))

    assert len(_gate_errors(caplog, cg)) == 2


def test_gate_targets_shares_the_audit_helper(tmp_path, monkeypatch, caplog):
    """One helper for both gates, so the treatment cannot drift apart."""
    cg = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(cg, "REVIEW_LOG", str(_blocked_log(tmp_path)))

    with caplog.at_level("ERROR", logger=cg.__name__):
        result = cg.gate_targets(["light.living_room"], name="n")
        cg.sentinel_review(VALID, _states("light.living_room"))

    assert result["action"] == "deny" and result["gate"] == "gate_targets"
    assert result["authored_by"] == "agent"
    # both gates share the one reported-path set, so still a single line
    assert len(_gate_errors(caplog, cg)) == 1


# ── Unreadable state files: observable, and not sticky ──────────────────
#
# If a READ path swallowed its OSError silently, and the legacy one
# cached the result on the CLASS -- once per process -- a single transient
# error (a permission blip, the trail locked by ha_device_watcher mid-write,
# ENFILE) would pin the process to "no legacy approvals exist" for its whole
# lifetime, and a corrupt ledger would revert every prior approve/deny to
# "pending" without a word. Both stay FAIL-CLOSED throughout -- nothing here
# may make an entity look approved. What matters is that the loss is
# reported, and that a failed read is retried on the next call instead of
# cached forever.


def _gate_warnings(caplog, cg):
    return [r for r in caplog.records
            if r.levelname == "WARNING" and r.name == cg.__name__]


def _unreadable_dir(tmp_path, name):
    """A state path that is a DIRECTORY, so ``open()`` fails for real.

    PermissionError on Windows, IsADirectoryError on POSIX -- both OSError,
    neither FileNotFoundError -- with no stdlib monkeypatched out from under
    the module. Returned so a test can rmdir it and let the retry succeed.
    """
    blocked = tmp_path / name
    blocked.mkdir(parents=True, exist_ok=True)
    return blocked


def test_missing_legacy_trail_is_silent_and_its_empty_result_is_cached(
        tmp_path, monkeypatch, caplog):
    """Absence is a definitive answer, not a failed read -- and it is the hot
    path on a fresh box, so it must neither log nor re-read every call."""
    cg = _load(tmp_path, monkeypatch)

    with caplog.at_level("WARNING", logger=cg.__name__):
        assert cg.get_store().status("light.living_room") == "pending"

    assert _gate_warnings(caplog, cg) == []
    assert isinstance(cg.ConsentStore._legacy_cache, set)
    assert cg.ConsentStore._legacy_cache == set()


def test_transient_legacy_read_error_warns_and_is_retried(
        tmp_path, monkeypatch, caplog):
    """The regression: one blip used to deny the blanket set until a restart."""
    cg = _load(tmp_path, monkeypatch)
    blocked = _unreadable_dir(tmp_path, "ha_device_approvals.jsonl")
    store = cg.get_store()

    with caplog.at_level("WARNING", logger=cg.__name__):
        assert store.status("light.living_room") == "pending"  # fail closed
        assert cg.ConsentStore._legacy_cache is None           # and NOT cached

        warnings = _gate_warnings(caplog, cg)
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert str(blocked) in msg
        assert "pending" in msg and "blanket" in msg

        # the error clears; the very next call re-reads rather than restarting
        blocked.rmdir()
        blocked.write_text(
            json.dumps({"entity_id": "light.living_room"}) + "\n",
            encoding="utf-8")
        assert store.status("light.living_room") == "legacy_blanket"


def test_repeated_legacy_read_error_still_warns_only_once(
        tmp_path, monkeypatch, caplog):
    """Retrying every call must not mean logging every call."""
    cg = _load(tmp_path, monkeypatch)
    _unreadable_dir(tmp_path, "ha_device_approvals.jsonl")
    store = cg.get_store()

    with caplog.at_level("WARNING", logger=cg.__name__):
        for _ in range(3):
            assert store.status("light.living_room") == "pending"

    assert len(_gate_warnings(caplog, cg)) == 1


class _TornRead:
    """A file object that yields one good line and then fails the read."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield json.dumps({"entity_id": "light.living_room"}) + "\n"
        raise OSError("read failed part-way through the trail")


def test_legacy_read_torn_mid_iteration_caches_nothing(
        tmp_path, monkeypatch, caplog):
    """A half-built set must never be cached: it would deny the other half."""
    cg = _load(tmp_path, monkeypatch)
    legacy = tmp_path / "ha_device_approvals.jsonl"
    legacy.write_text("", encoding="utf-8")
    real_open = builtins.open

    def fake_open(file, *args, **kwargs):
        if str(file) == str(legacy):
            return _TornRead()
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)

    with caplog.at_level("WARNING", logger=cg.__name__):
        # even the entity on the line that DID parse stays pending
        assert cg.get_store().status("light.living_room") == "pending"

    assert cg.ConsentStore._legacy_cache is None
    assert len(_gate_warnings(caplog, cg)) == 1


def test_corrupt_ledger_reverts_to_pending_loudly_and_once(
        tmp_path, monkeypatch, caplog):
    """A truncated ledger is not "no ledger yet" -- it wants a restore."""
    cg = _load(tmp_path, monkeypatch)
    cg.get_store().approve("light.kitchen")
    ledger = tmp_path / "device_consent.json"
    assert json.loads(ledger.read_text(encoding="utf-8"))["light.kitchen"]
    ledger.write_text('{"light.kitchen": {"status": "appr', encoding="utf-8")

    with caplog.at_level("ERROR", logger=cg.__name__):
        first = cg.ConsentStore()
        second = cg.ConsentStore()

    assert first.status("light.kitchen") == "pending"   # fail closed
    assert second.status("light.kitchen") == "pending"
    errors = _gate_errors(caplog, cg)
    assert len(errors) == 1                             # once per path
    msg = errors[0].getMessage()
    assert str(ledger) in msg
    assert "corrupt" in msg and "pending" in msg


def test_ledger_that_is_valid_json_but_not_an_object_is_corrupt_too(
        tmp_path, monkeypatch, caplog):
    """An interrupted write can leave a list behind; status() used to die on
    AttributeError far from the read that caused it."""
    cg = _load(tmp_path, monkeypatch)
    ledger = tmp_path / "device_consent.json"
    ledger.write_text("[]", encoding="utf-8")

    with caplog.at_level("ERROR", logger=cg.__name__):
        assert cg.ConsentStore().status("light.kitchen") == "pending"

    errors = _gate_errors(caplog, cg)
    assert len(errors) == 1
    assert "expected a JSON object" in errors[0].getMessage()


def test_absent_ledger_is_silent(tmp_path, monkeypatch, caplog):
    """A fresh box has no ledger; that is normal, not an incident."""
    cg = _load(tmp_path, monkeypatch)

    with caplog.at_level("ERROR", logger=cg.__name__):
        assert cg.ConsentStore().status("light.kitchen") == "pending"

    assert _gate_errors(caplog, cg) == []


def _deny_reads_of(monkeypatch, path):
    """Make ``open()`` fail on one existing file, leaving every other read real.

    The directory trick cannot serve here: file-level adoption only fires on an
    ``os.path.isfile`` legacy path, so the file has to genuinely exist AND fail
    to open -- which on Windows means an ACL, or this.
    """
    real_open = builtins.open

    def fake_open(file, *args, **kwargs):
        if str(file) == str(path):
            raise PermissionError(13, "Permission denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)


def test_the_adoption_warning_is_rate_limited_too(tmp_path, monkeypatch, caplog):
    """Retrying the read must not re-warn about the path it resolved through.

    Dropping the cache-on-failure turned _state_file()'s adoption warning from
    a once-per-process line into a once-per-status() one, and it fires exactly
    when the trail is unreadable AND adopted -- the "locked by
    ha_device_watcher mid-write" case. One gate call over three targets emitted
    ten of them. Uses ``_load_unset`` (defined below) because adoption is
    suppressed whenever $PUFFPAI_DATA_DIR is set.
    """
    cg = _load_unset(tmp_path, monkeypatch)
    legacy = _seed(tmp_path / "old", "ha_device_approvals.jsonl",
                   json.dumps({"entity_id": "light.living_room"}) + "\n")
    _deny_reads_of(monkeypatch, legacy)
    store = cg.ConsentStore()

    with caplog.at_level("WARNING", logger=cg.__name__):
        for _ in range(3):
            assert store.status("light.living_room") == "pending"

    messages = [r.getMessage() for r in _gate_warnings(caplog, cg)]
    assert len([m for m in messages if "unreadable" in m]) == 1
    assert len([m for m in messages if "using legacy consent state file" in m]) == 1


def test_a_malformed_record_is_dropped_without_taking_the_ledger_with_it(
        tmp_path, monkeypatch, caplog):
    """Per-RECORD damage stays per-record: 103 approvals must not die for one.

    The record is valid JSON inside a valid JSON object, so neither shape check
    at the file level sees it; status() used to raise AttributeError on it.
    """
    cg = _load(tmp_path, monkeypatch)
    ledger = tmp_path / "device_consent.json"
    ledger.write_text(json.dumps({
        "light.kitchen": "approved",                    # malformed: a string
        "light.living_room": {"status": "approved"},
        "lock.front": {"status": "denied"},
    }), encoding="utf-8")

    with caplog.at_level("WARNING", logger=cg.__name__):
        store = cg.ConsentStore()
        assert store.status("light.kitchen") == "pending"       # fail closed
        assert store.status("light.living_room") == "approved"  # survives
        assert store.status("lock.front") == "denied"

    warnings = [r.getMessage() for r in _gate_warnings(caplog, cg)]
    assert len(warnings) == 1
    assert str(ledger) in warnings[0]
    assert "1 malformed record" in warnings[0]
    # a per-record fault is not the whole-file treatment
    assert _gate_errors(caplog, cg) == []


def _corrupt(ledger):
    ledger.write_text('{"light.b": {"status": "appr', encoding="utf-8")


def _one_bad_record(ledger):
    ledger.write_text(json.dumps({
        "light.a": "approved",                      # malformed: a string
        "light.b": {"status": "approved"},
    }), encoding="utf-8")


def test_a_record_warning_does_not_silence_the_corrupt_file_error(
        tmp_path, monkeypatch, caplog):
    """Two severities on one path need two keys, or the benign one wins.

    The dropped-record WARNING is survivable; the corrupt-file ERROR is the one
    that says "restore a backup". Sharing a set let the first permanently
    silence the second on the same path.
    """
    cg = _load(tmp_path, monkeypatch)
    ledger = tmp_path / "device_consent.json"

    with caplog.at_level("WARNING", logger=cg.__name__):
        _one_bad_record(ledger)
        assert cg.ConsentStore().status("light.b") == "approved"
        _corrupt(ledger)
        assert cg.ConsentStore().status("light.b") == "pending"

    assert len(_gate_warnings(caplog, cg)) == 1
    assert len(_gate_errors(caplog, cg)) == 1


def test_a_corrupt_file_error_does_not_silence_a_later_record_warning(
        tmp_path, monkeypatch, caplog):
    """The suppression runs both ways; so does the fix."""
    cg = _load(tmp_path, monkeypatch)
    ledger = tmp_path / "device_consent.json"

    with caplog.at_level("WARNING", logger=cg.__name__):
        _corrupt(ledger)
        assert cg.ConsentStore().status("light.b") == "pending"
        _one_bad_record(ledger)
        assert cg.ConsentStore().status("light.b") == "approved"

    assert len(_gate_errors(caplog, cg)) == 1
    assert len(_gate_warnings(caplog, cg)) == 1


def test_a_non_utf8_ledger_is_corrupt_not_a_crash(tmp_path, monkeypatch, caplog):
    """UnicodeDecodeError is a ValueError -- it escaped both handlers."""
    cg = _load(tmp_path, monkeypatch)
    ledger = tmp_path / "device_consent.json"
    ledger.write_bytes(b'{"light.kitchen": {"status": "appro\xffved"}}')

    with caplog.at_level("ERROR", logger=cg.__name__):
        assert cg.ConsentStore().status("light.kitchen") == "pending"

    errors = _gate_errors(caplog, cg)
    assert len(errors) == 1
    assert "not valid UTF-8" in errors[0].getMessage()


def test_a_non_utf8_legacy_trail_is_a_failed_read(tmp_path, monkeypatch, caplog):
    """Same escape on the other file; same fail-closed, non-caching bucket."""
    cg = _load(tmp_path, monkeypatch)
    (tmp_path / "ha_device_approvals.jsonl").write_bytes(
        b'{"entity_id": "light.li\xffving_room"}\n')

    with caplog.at_level("WARNING", logger=cg.__name__):
        assert cg.get_store().status("light.living_room") == "pending"

    assert cg.ConsentStore._legacy_cache is None    # a failed read, so retried
    warnings = _gate_warnings(caplog, cg)
    assert len(warnings) == 1
    assert "unreadable" in warnings[0].getMessage()


def test_a_legacy_row_that_is_not_an_object_is_skipped_like_junk(
        tmp_path, monkeypatch, caplog):
    """`null` and `[1,2]` parse fine, then raise AttributeError on .get().

    It escaped before _legacy_cache was ever assigned, so EVERY later gated
    request raised too -- permanent denial by traceback until someone repaired
    the file by hand.
    """
    cg = _load(tmp_path, monkeypatch)
    (tmp_path / "ha_device_approvals.jsonl").write_text(
        "null\n"
        "[1, 2]\n"
        '"a bare string"\n'
        "{ not json at all\n"
        + json.dumps({"entity_id": "light.living_room"}) + "\n",
        encoding="utf-8")

    with caplog.at_level("WARNING", logger=cg.__name__):
        store = cg.get_store()
        # the good line still counts, and the bad ones cost nothing
        assert store.status("light.living_room") == "legacy_blanket"
        assert store.status("light.kitchen") == "pending"

    assert cg.ConsentStore._legacy_cache == {"light.living_room"}
    assert _gate_warnings(caplog, cg) == []


# ─────────────────── State-dir resolution ───────────────────
#
# CONSENT_PATH / REVIEW_LOG / LEGACY_APPROVALS must not be built at import from
# a raw $PUFFPAI_DATA_DIR. A relative value would go to the process CWD and a "~"
# value would be taken literally, so the consent ledger and the review trail
# could land somewhere the hardened unit cannot write -- silently, since every
# write site swallows OSError. They resolve through config.resolve_state_dir.


def _repo_root(cg) -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(cg.__file__)))))


def test_relative_data_dir_lands_under_the_repo_root(monkeypatch):
    cg = _load("relstate", monkeypatch)
    expected = os.path.join(_repo_root(cg), "relstate")
    assert cg.CONSENT_PATH == os.path.join(expected, "device_consent.json")
    assert cg.REVIEW_LOG == os.path.join(expected, "sentinel_reviews.jsonl")
    assert cg.ConsentStore().path == cg.CONSENT_PATH


def test_tilde_data_dir_is_expanded(monkeypatch):
    cg = _load("~/puff_state", monkeypatch)
    assert "~" not in cg.CONSENT_PATH
    assert cg.CONSENT_PATH == os.path.join(
        os.path.expanduser("~"), "puff_state", "device_consent.json")


def test_whitespace_only_data_dir_falls_back_to_the_repo_default(
        tmp_path, monkeypatch):
    """A blank-but-set value must not relocate state to a junk directory.

    The default is ``<repo>/data``, no longer the
    package-relative ``src/puffpai/data``. Blank counts as unset, which also
    arms file-level legacy adoption, so the legacy dir is redirected at an
    empty tmp dir -- this test pins the default, not what happens to be
    sitting in the old directory on this machine.
    """
    cg = _load("   ", monkeypatch)
    monkeypatch.setattr(cg, "_LEGACY_DATA_DIR", str(tmp_path / "empty"))
    assert cg.CONSENT_PATH == os.path.join(
        cg._DEFAULT_DATA_DIR, "device_consent.json")


def test_legacy_path_constants_are_still_attributes(tmp_path, monkeypatch):
    """self_check reads consent_gate.CONSENT_PATH as an attribute at call time."""
    cg = _load(tmp_path, monkeypatch)
    assert cg.CONSENT_PATH == os.path.join(str(tmp_path), "device_consent.json")
    assert cg.REVIEW_LOG == os.path.join(str(tmp_path), "sentinel_reviews.jsonl")
    assert cg.LEGACY_APPROVALS == os.path.join(
        str(tmp_path), "ha_device_approvals.jsonl")
    with pytest.raises(AttributeError):
        cg.NO_SUCH_PATH


def test_review_log_attribute_override_still_redirects_writes(tmp_path, monkeypatch):
    """Four suites isolate the audit trail by patching the module attribute."""
    cg = _load(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere" / "reviews.jsonl"
    elsewhere.parent.mkdir()
    monkeypatch.setattr(cg, "REVIEW_LOG", str(elsewhere))

    cg.sentinel_review(VALID, _states("light.living_room"))

    assert elsewhere.is_file()
    assert not (tmp_path / "sentinel_reviews.jsonl").exists()


# ─────────── Legacy adoption of the old default ───────────
#
# The env-unset default moved from src/puffpai/data to <repo>/data, so the gate
# reads the ha_device_approvals.jsonl the watcher actually writes. A box that
# already holds a consent ledger or a review trail in the old directory must
# not come back empty: adoption is per FILE (<repo>/data exists in every
# checkout, so the canon's directory-level probe never fires) and read-only --
# it points at the old file and warns; it never copies or moves it.
#
# _load() SETS $PUFFPAI_DATA_DIR, which suppresses adoption by design, so these
# cases reload with it unset and redirect the two directory constants instead.


def _load_unset(tmp_path, monkeypatch):
    """Reload the gate with the env var unset and both dirs under tmp_path.

    ``new`` is created empty on purpose: <repo>/data exists in every checkout,
    so leaving it absent would let the canon's directory-level ``legacy=``
    answer instead, and these cases would never reach the file-level rule they
    exist to pin.
    """
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    monkeypatch.setenv("PUFFPAI_ENFORCE_CONSENT", "0")
    import puffpai.security.consent_gate as cg
    importlib.reload(cg)
    cg._store = None
    cg.ConsentStore._legacy_cache = None
    (tmp_path / "new").mkdir(exist_ok=True)
    monkeypatch.setattr(cg, "_DEFAULT_DATA_DIR", str(tmp_path / "new"))
    monkeypatch.setattr(cg, "_LEGACY_DATA_DIR", str(tmp_path / "old"))
    monkeypatch.setattr(cg, "_data_dir_cache", None)
    return cg


def _seed(directory, name, text):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")
    return directory / name


def test_legacy_consent_ledger_is_adopted_when_the_new_path_has_none(
        tmp_path, monkeypatch):
    """Losing the ledger turns every prior approve/deny back into "pending"."""
    cg = _load_unset(tmp_path, monkeypatch)
    legacy = _seed(tmp_path / "old", "device_consent.json", "{}")

    assert cg.CONSENT_PATH == str(legacy)


def test_a_consent_ledger_at_the_new_path_wins(tmp_path, monkeypatch):
    cg = _load_unset(tmp_path, monkeypatch)
    _seed(tmp_path / "old", "device_consent.json", "{}")
    current = _seed(tmp_path / "new", "device_consent.json", "{}")

    assert cg.CONSENT_PATH == str(current)


def test_env_set_suppresses_consent_ledger_adoption(tmp_path, monkeypatch):
    """$PUFFPAI_DATA_DIR is an operator's explicit choice; a stale file in the
    code tree must not quietly win over it."""
    cg = _load(tmp_path / "state", monkeypatch)
    legacy = _seed(tmp_path / "old", "device_consent.json", "{}")
    monkeypatch.setattr(cg, "_LEGACY_DATA_DIR", str(tmp_path / "old"))
    monkeypatch.setattr(cg, "_data_dir_cache", None)

    assert cg.CONSENT_PATH == os.path.join(
        str(tmp_path / "state"), "device_consent.json")
    assert cg.CONSENT_PATH != str(legacy)


def test_legacy_review_log_is_adopted_and_appended_to(tmp_path, monkeypatch):
    """The audit trail stays one file rather than silently starting over."""
    cg = _load_unset(tmp_path, monkeypatch)
    legacy = _seed(tmp_path / "old", "sentinel_reviews.jsonl", "")

    assert cg.REVIEW_LOG == str(legacy)
    cg.sentinel_review(VALID, _states("light.living_room"))
    assert len(legacy.read_text(encoding="utf-8").strip().splitlines()) == 1
    assert not (tmp_path / "new" / "sentinel_reviews.jsonl").exists()


def test_env_set_suppresses_review_log_adoption(tmp_path, monkeypatch):
    cg = _load(tmp_path / "state", monkeypatch)
    _seed(tmp_path / "old", "sentinel_reviews.jsonl", "")
    monkeypatch.setattr(cg, "_LEGACY_DATA_DIR", str(tmp_path / "old"))
    monkeypatch.setattr(cg, "_data_dir_cache", None)

    assert cg.REVIEW_LOG == os.path.join(
        str(tmp_path / "state"), "sentinel_reviews.jsonl")


def test_consent_store_reads_an_adopted_legacy_ledger(tmp_path, monkeypatch):
    """Adoption at the byte level: a prior approval survives the move."""
    cg = _load_unset(tmp_path, monkeypatch)
    _seed(tmp_path / "old", "device_consent.json",
          json.dumps({"light.kitchen": {"status": "approved"}}))

    store = cg.ConsentStore()

    assert store.status("light.kitchen") == "approved"


# ── consent_requests: same resolver, same guarantees ────────────────

def _load_requests(data_dir, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(data_dir))
    import puffpai.security.consent_requests as cr
    importlib.reload(cr)
    return cr


def test_requests_path_follows_the_data_dir(tmp_path, monkeypatch):
    cr = _load_requests(tmp_path, monkeypatch)
    assert cr.REQUESTS_PATH == os.path.join(str(tmp_path), "consent_requests.json")
    # and the queue round-trips through it
    cr._save([{"entity_id": "camera.shed", "status": "open"}])
    assert (tmp_path / "consent_requests.json").is_file()
    assert cr._load()[0]["entity_id"] == "camera.shed"


def test_requests_relative_data_dir_lands_under_the_repo_root(monkeypatch):
    cr = _load_requests("relstate", monkeypatch)
    assert cr.REQUESTS_PATH == os.path.join(
        _repo_root(cr), "relstate", "consent_requests.json")


def test_requests_tilde_data_dir_is_expanded(monkeypatch):
    cr = _load_requests("~/puff_state", monkeypatch)
    assert "~" not in cr.REQUESTS_PATH
    assert cr.REQUESTS_PATH == os.path.join(
        os.path.expanduser("~"), "puff_state", "consent_requests.json")


def test_requests_activity_log_follows_the_data_dir(tmp_path, monkeypatch):
    """The activity feed is state, not a cross-repo path.

    If it were pinned dirnames up in a sibling checkout, on the
    appliance that would resolve to a path absent and uncreatable under
    ProtectSystem=strict, so `except OSError: pass` would drop every consent
    request and every verdict. See tests/test_agent_activity.py for the feed's
    own resolution rules; this pins that the consent loop uses them.
    """
    import puffpai.agent_activity as aa
    cr = _load_requests(tmp_path, monkeypatch)
    aa.reset_cache()  # after the env moves: the feed caches its resolved path
    assert cr.ACTIVITY_LOG == os.path.join(str(tmp_path), "agent-activity.jsonl")
    assert "sibling-checkout" not in cr.ACTIVITY_LOG


# ─────── consent_requests: default at <repo>/data ───────
#
# The queue's env-unset default moved from src/puffpai/data to <repo>/data,
# joining consent_gate. The two files are halves of one decision record --
# resolve() closes the request here and then records the verdict through
# consent_gate.get_store() -- so a split across two directories meant relocating
# or backing up one directory captured half of it. A box that already holds a
# pending queue in the old directory must not come back with an empty one (the
# user would never be asked again about a device Sentinel already blocked), so
# adoption is per FILE and read-only: it points at the old file and warns, and
# never copies, moves, or creates anything there.
#
# Legacy adoption is opt-in per test, never ambient: this checkout really does
# have files under src/puffpai/data, so every case below repoints
# _LEGACY_DATA_DIR at a tmp directory and clears _data_dir_cache.


def _load_requests_unset(tmp_path, monkeypatch):
    """Reload the queue with the env var unset and both dirs under tmp_path.

    ``new`` is created empty on purpose: <repo>/data exists in every checkout,
    so leaving it absent would let the canon's directory-level ``legacy=``
    answer first, and these cases would never reach the file-level rule they
    exist to pin.
    """
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    import puffpai.security.consent_requests as cr
    importlib.reload(cr)
    (tmp_path / "new").mkdir(exist_ok=True)
    monkeypatch.setattr(cr, "_DEFAULT_DATA_DIR", str(tmp_path / "new"))
    monkeypatch.setattr(cr, "_LEGACY_DATA_DIR", str(tmp_path / "old"))
    monkeypatch.setattr(cr, "_data_dir_cache", None)
    return cr


def _neutralise_legacy(cr, tmp_path, monkeypatch):
    """Point the legacy dir at somewhere absent, keeping the real default."""
    monkeypatch.setattr(cr, "_LEGACY_DATA_DIR",
                        str(tmp_path / "absent_legacy"))
    monkeypatch.setattr(cr, "_data_dir_cache", None)


def test_requests_unset_env_lands_in_the_repo_data_dir(tmp_path, monkeypatch):
    """The new default: <repo>/data, not the package-relative src/puffpai/data.

    Legacy is neutralised so this pins the default itself rather than whatever
    happens to be sitting in the old directory on this machine.
    """
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    import puffpai.security.consent_requests as cr
    importlib.reload(cr)
    _neutralise_legacy(cr, tmp_path, monkeypatch)

    assert cr._DEFAULT_DATA_DIR == os.path.join(_repo_root(cr), "data")
    # four dirnames up really is the repo root, not <repo>/src
    assert os.path.isdir(os.path.join(cr._REPO_ROOT, "src", "puffpai"))
    assert cr.REQUESTS_PATH == os.path.join(
        cr._DEFAULT_DATA_DIR, "consent_requests.json")
    # and it agrees with the ledger's directory, which is the point of the move
    import puffpai.security.consent_gate as cg
    assert cr._DEFAULT_DATA_DIR == cg._DEFAULT_DATA_DIR


def test_legacy_request_queue_is_adopted_when_the_new_path_has_none(
        tmp_path, monkeypatch):
    """An upgraded box must not silently forget what Sentinel already asked."""
    cr = _load_requests_unset(tmp_path, monkeypatch)
    legacy = _seed(tmp_path / "old", "consent_requests.json", "[]")

    assert cr.REQUESTS_PATH == str(legacy)


def test_a_request_queue_at_the_new_path_wins(tmp_path, monkeypatch):
    cr = _load_requests_unset(tmp_path, monkeypatch)
    _seed(tmp_path / "old", "consent_requests.json", "[]")
    current = _seed(tmp_path / "new", "consent_requests.json", "[]")

    assert cr.REQUESTS_PATH == str(current)


def test_env_set_suppresses_request_queue_adoption(tmp_path, monkeypatch):
    """$PUFFPAI_DATA_DIR is an operator's explicit choice; a stale queue in the
    code tree must not quietly win over it."""
    cr = _load_requests(tmp_path / "state", monkeypatch)
    legacy = _seed(tmp_path / "old", "consent_requests.json", "[]")
    monkeypatch.setattr(cr, "_LEGACY_DATA_DIR", str(tmp_path / "old"))
    monkeypatch.setattr(cr, "_data_dir_cache", None)

    assert cr.REQUESTS_PATH == os.path.join(
        str(tmp_path / "state"), "consent_requests.json")
    assert cr.REQUESTS_PATH != str(legacy)


@pytest.mark.parametrize("form", ["absolute", "relative"])
def test_env_set_to_the_default_dir_itself_still_suppresses_adoption(
        form, tmp_path, monkeypatch):
    """The regression this rule exists for: SET, not "resolved somewhere else".

    Pointing $PUFFPAI_DATA_DIR at the default data dir -- absolute <repo>/data,
    or the relative "data", which joins to the repo root -- resolves to exactly
    the shipped directory. An implementation that decides "did the dir move?"
    by comparing paths reads that as unset, adopts the legacy queue, and then
    reads AND WRITES pending consent requests inside the read-only code tree
    while an env var explicitly says otherwise.
    """
    import puffpai.security.consent_requests as cr_mod
    repo_data = os.path.join(_repo_root(cr_mod), "data")
    cr = _load_requests(repo_data if form == "absolute" else "data", monkeypatch)
    legacy = _seed(tmp_path / "old", "consent_requests.json", "[]")
    monkeypatch.setattr(cr, "_LEGACY_DATA_DIR", str(tmp_path / "old"))
    monkeypatch.setattr(cr, "_data_dir_cache", None)

    assert cr._data_dir() == repo_data, "the repro needs the dirs to coincide"
    assert cr.REQUESTS_PATH == os.path.join(repo_data, "consent_requests.json")
    assert cr.REQUESTS_PATH != str(legacy)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_data_dir_counts_as_unset_for_request_adoption(
        blank, tmp_path, monkeypatch):
    """Blank-but-set is "unset" everywhere in the canon, adoption included."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", blank)
    import puffpai.security.consent_requests as cr
    importlib.reload(cr)
    (tmp_path / "new").mkdir(exist_ok=True)
    monkeypatch.setattr(cr, "_DEFAULT_DATA_DIR", str(tmp_path / "new"))
    monkeypatch.setattr(cr, "_LEGACY_DATA_DIR", str(tmp_path / "old"))
    monkeypatch.setattr(cr, "_data_dir_cache", None)
    legacy = _seed(tmp_path / "old", "consent_requests.json", "[]")

    assert cr.REQUESTS_PATH == str(legacy)


def test_requests_path_override_wins_over_adoption(
        tmp_path, monkeypatch, request):
    """The explicit override outranks everything, legacy adoption included.

    Assigned straight into the module globals (which is what an override IS --
    it shadows the PEP 562 ``__getattr__``) and popped afterwards. monkeypatch
    would restore the *resolved* path as a real global on undo, leaving a
    hard-coded constant behind for anything that does not reload.
    """
    cr = _load_requests_unset(tmp_path, monkeypatch)
    _seed(tmp_path / "old", "consent_requests.json", "[]")
    elsewhere = tmp_path / "elsewhere" / "queue.json"
    request.addfinalizer(lambda: cr.__dict__.pop("REQUESTS_PATH", None))
    cr.REQUESTS_PATH = str(elsewhere)

    assert cr._requests_path() == str(elsewhere)
    assert cr.REQUESTS_PATH == str(elsewhere)


def test_an_adopted_queue_is_read_and_written_in_place(tmp_path, monkeypatch):
    """Adoption at the byte level: the old file stays the one queue.

    Nothing is copied or created at the new path -- the operator moves it when
    they choose, and until then both halves of the request keep working.
    """
    cr = _load_requests_unset(tmp_path, monkeypatch)
    monkeypatch.setattr(cr, "ACTIVITY_LOG", str(tmp_path / "activity.jsonl"))
    legacy = _seed(tmp_path / "old", "consent_requests.json", json.dumps(
        [{"entity_id": "camera.shed", "label": "Shed cam", "action": "view",
          "status": "open"}]))

    # read: the pending ask survives the move
    assert [r["entity_id"] for r in cr.list_open()] == ["camera.shed"]

    # write: a new request lands in the adopted file, not at the new path
    cr.create_request("lock.front", "Front lock", "unlock", "asked in chat")
    rows = json.loads(legacy.read_text(encoding="utf-8"))
    assert [r["entity_id"] for r in rows] == ["camera.shed", "lock.front"]
    assert not (tmp_path / "new" / "consent_requests.json").exists()
