"""Zone 3 state-dir resolution — where the DNS rule audit trail lands.

The AdGuard controller is MONITOR-FIRST: with ``enforce=False`` the JSONL
ledger is the *only* record of what it would have blocked, so its location is
security-relevant. It used to resolve at import from a raw $PUFFPAI_DATA_DIR
(no strip, no "~" expansion, a relative value taken against the process CWD)
against a "../data" default, and every write site swallows OSError — so a
misplaced path lost the trail silently. It now goes through the one resolver,
:func:`puffpai.config.resolve_state_dir`, reached via
:func:`puffpai.statedir.resolve_runtime_dir`, the shared stdlib-only leaf.

No network: only the path helpers and ``_audit`` are exercised, never the
REST client. Run: pytest tests/test_zone3_paths.py -q
"""

import json
import os

import pytest

from puffpai.config import resolve_state_dir
from puffpai.zone3 import adguard_controller as ac


@pytest.fixture(autouse=True)
def _unresolved(monkeypatch):
    """Drop the cached path so each case re-resolves from the env."""
    monkeypatch.setattr(ac, "_rules_audit", None)


def _expected_dir() -> str:
    return resolve_state_dir(
        None, repo_root=ac._REPO_ROOT, default=os.path.join(ac._REPO_ROOT, "data"))


@pytest.mark.parametrize("env_value", [
    None,            # unset
    "",
    "   ",
    "relstate",
    "~/puff_state",
    "<abs>",         # substituted with tmp_path below
])
def test_rules_audit_path_matches_the_canonical_resolver(
        env_value, tmp_path, monkeypatch):
    if env_value == "<abs>":
        env_value = str(tmp_path / "state")
    if env_value is None:
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("PUFFPAI_DATA_DIR", env_value)

    assert ac._rules_audit_path() == os.path.join(
        _expected_dir(), "zone3_dns_rules.jsonl")


def test_unset_env_lands_in_the_repo_data_dir(monkeypatch):
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    assert ac._rules_audit_path() == os.path.join(
        ac._REPO_ROOT, "data", "zone3_dns_rules.jsonl")


def test_relative_env_joins_the_repo_root_not_the_cwd(monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", "relstate")
    assert ac._rules_audit_path() == os.path.join(
        ac._REPO_ROOT, "relstate", "zone3_dns_rules.jsonl")


def test_tilde_env_is_expanded(monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", "~/puff_state")
    resolved = ac._rules_audit_path()
    assert "~" not in resolved
    assert resolved == os.path.join(
        os.path.expanduser("~"), "puff_state", "zone3_dns_rules.jsonl")


def test_path_is_resolved_once_and_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    first = ac._rules_audit_path()
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "moved"))
    assert ac._rules_audit_path() == first, "cached after first use"


def test_audit_writes_jsonl_to_the_resolved_path(tmp_path, monkeypatch):
    """Monitor-first evidence actually reaches the relocated state dir."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    controller = ac.AdGuardController("http://127.0.0.1:3000", enforce=False)

    controller._audit("block_domain", {"domain": "telemetry.example.com"}, False)

    ledger = tmp_path / "state" / "zone3_dns_rules.jsonl"
    assert ledger.is_file()
    row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    assert row["action"] == "block_domain"
    assert row["applied"] is False
    assert row["enforce_mode"] is False


# ---------------------------------------------------------------------------
# PolicyEngine — the managed-device ledger
# ---------------------------------------------------------------------------
#
# This ledger records which devices are in LOCKDOWN and, more importantly,
# which are ALWAYS_ALLOW: medical devices and fire alarms that must never be
# DNS-blocked. If _save_managed_devices() cannot write (it swallows IOError
# and only logs), the next restart comes back with an empty policy set and
# those markings are gone. So where this file lands is a safety property, not
# a tidiness one.

from pathlib import Path                                      # noqa: E402
from puffpai.zone3 import policy_engine as pe                 # noqa: E402


@pytest.fixture(autouse=True)
def _unresolved_policy_path(monkeypatch, tmp_path):
    """Drop the cached ledger path so each case re-resolves from the env.

    Both overrides are neutralised too: the ops hook, and the legacy
    <repo>/src/data ledger that a dev box that ran an older build really has.
    Legacy adoption is opt-in per test, never ambient — otherwise a case's
    answer would depend on real state sitting in the checkout.
    """
    monkeypatch.setattr(pe, "_managed_devices_file", None)
    monkeypatch.setattr(pe, "_MANAGED_DEVICES_FILE", None)
    monkeypatch.setattr(pe, "_LEGACY_MANAGED_DEVICES_FILE",
                        tmp_path / "absent_legacy" / "zone3_managed_devices.json")


class TestManagedDevicesPath:
    def test_unset_env_lands_in_the_repo_data_dir(self, monkeypatch):
        """The unset-env default, and the one thing that DID move here.

        The pre-migration constant was ``Path(__file__).resolve().parent.
        parent.parent / "data" / ...`` from src/puffpai/zone3/policy_engine.py
        -- three parents up is <repo>/src, NOT the repo root, so the ledger
        went to <repo>/src/data despite the old ``_REPO_ROOT`` name promising
        <repo>/data. That off-by-one was corrected rather than preserved; the
        old location survives as a read-only legacy fallback (see below), so
        an existing box's ledger is adopted rather than orphaned.
        """
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        resolved = pe._managed_devices_path()
        assert resolved == (Path(pe._REPO_ROOT) / "data"
                            / "zone3_managed_devices.json")
        assert pe._REPO_ROOT == pe._SRC_DIR.parent
        assert resolved.parent.parent.name != "src", "the off-by-one is fixed"

    def test_legacy_ledger_is_adopted_when_the_new_path_has_none(
            self, tmp_path, monkeypatch):
        """An upgraded box must not start from an empty policy set.

        Losing the ledger means every ALWAYS_ALLOW marking (medical devices,
        fire alarms) is gone, so the fallback is the safety-relevant half of
        moving the default. Redirected at tmp_path rather than seeding a file
        into the real <repo>/src/data.
        """
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        legacy = tmp_path / "src" / "data" / "zone3_managed_devices.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(pe, "_DEFAULT_DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(pe, "_LEGACY_MANAGED_DEVICES_FILE", legacy)

        assert pe._managed_devices_path() == legacy

    def test_legacy_is_ignored_once_the_new_ledger_exists(
            self, tmp_path, monkeypatch):
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        legacy = tmp_path / "src" / "data" / "zone3_managed_devices.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("[]", encoding="utf-8")
        current = tmp_path / "data" / "zone3_managed_devices.json"
        current.parent.mkdir(parents=True)
        current.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(pe, "_DEFAULT_DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(pe, "_LEGACY_MANAGED_DEVICES_FILE", legacy)

        assert pe._managed_devices_path() == current

    def test_legacy_never_overrides_an_explicit_state_dir(
            self, tmp_path, monkeypatch):
        """$PUFFPAI_DATA_DIR is an operator's explicit choice; a stale file in
        the code tree must not quietly win over it."""
        legacy = tmp_path / "src" / "data" / "zone3_managed_devices.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(pe, "_LEGACY_MANAGED_DEVICES_FILE", legacy)
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))

        assert pe._managed_devices_path() == (
            tmp_path / "state" / "zone3_managed_devices.json")

    def test_env_pointed_at_the_default_dir_still_suppresses_adoption(
            self, tmp_path, monkeypatch, caplog):
        """The env var being SET is what suppresses adoption -- not the dir
        having moved.

        An operator who sets $PUFFPAI_DATA_DIR to the default data dir gets a
        resolved dir identical to the shipped one. Deciding by dir comparison
        reads that as "unset", adopts, and the engine then reads AND WRITES the
        ledger in the read-only code tree while an env var says otherwise. The
        canon has the same shape: resolve_state_dir returns on a non-empty
        env_root before it ever probes legacy.
        """
        legacy = tmp_path / "src" / "data" / "zone3_managed_devices.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(pe, "_LEGACY_MANAGED_DEVICES_FILE", legacy)
        monkeypatch.setattr(pe, "_DEFAULT_DATA_DIR", tmp_path / "data")
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "data"))

        with caplog.at_level("WARNING"):
            resolved = pe._managed_devices_path()
        assert resolved == tmp_path / "data" / "zone3_managed_devices.json"
        assert resolved != legacy, "env was set; the code tree must not win"
        assert "legacy" not in caplog.text

    def test_env_set_to_the_real_repo_data_dir_suppresses_adoption(
            self, tmp_path, monkeypatch):
        """Same repro against the REAL default, the way an operator hits it."""
        from puffpai import statedir
        legacy = tmp_path / "src" / "data" / "zone3_managed_devices.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(pe, "_LEGACY_MANAGED_DEVICES_FILE", legacy)
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(pe._DEFAULT_DATA_DIR))

        assert pe._managed_devices_path() == (
            Path(statedir._REPO_ROOT) / "data" / "zone3_managed_devices.json")

    def test_relative_env_naming_the_data_dir_suppresses_adoption(
            self, tmp_path, monkeypatch):
        """PUFFPAI_DATA_DIR="data" joins the repo root, landing exactly on the
        shipped default -- the second shape of the same repro."""
        from puffpai import statedir
        legacy = tmp_path / "src" / "data" / "zone3_managed_devices.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(pe, "_LEGACY_MANAGED_DEVICES_FILE", legacy)
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "data")

        resolved = pe._managed_devices_path()
        assert resolved == (Path(statedir._REPO_ROOT) / "data"
                            / "zone3_managed_devices.json")
        assert resolved != legacy

    @pytest.mark.parametrize("env_value", ["", "   "])
    def test_a_blank_env_var_is_unset_for_adoption_too(
            self, env_value, tmp_path, monkeypatch):
        """Canon treats empty/whitespace as unset; adoption must agree."""
        legacy = tmp_path / "src" / "data" / "zone3_managed_devices.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(pe, "_LEGACY_MANAGED_DEVICES_FILE", legacy)
        monkeypatch.setattr(pe, "_DEFAULT_DATA_DIR", tmp_path / "data")
        monkeypatch.setenv("PUFFPAI_DATA_DIR", env_value)

        assert pe._managed_devices_path() == legacy

    def test_explicit_override_hook_wins_over_everything(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        monkeypatch.setattr(pe, "_MANAGED_DEVICES_FILE", tmp_path / "pinned.json")
        assert pe._managed_devices_path() == tmp_path / "pinned.json"

    def test_a_junk_override_hook_is_ignored(self, tmp_path, monkeypatch):
        """A bare MagicMock satisfies os.fspath with a junk name; Path() on it
        raises outright. Ignore it and resolve as if unset."""
        from unittest import mock
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        monkeypatch.setattr(pe, "_MANAGED_DEVICES_FILE", mock.MagicMock())

        assert pe._managed_devices_path() == (
            tmp_path / "state" / "zone3_managed_devices.json")

    def test_env_set_puts_the_ledger_in_the_state_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        assert pe._managed_devices_path() == (
            tmp_path / "state" / "zone3_managed_devices.json")

    def test_relative_env_joins_the_repo_root_not_the_cwd(self, monkeypatch):
        """The repo root here is the REAL one, from puffpai.statedir -- not
        this module's mislabelled <repo>/src default."""
        from puffpai import statedir
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "relstate")
        assert pe._managed_devices_path() == (
            Path(statedir._REPO_ROOT) / "relstate" / "zone3_managed_devices.json")

    def test_tilde_env_is_expanded(self, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "~/puff_state")
        resolved = pe._managed_devices_path()
        assert "~" not in str(resolved)
        assert resolved == (Path(os.path.expanduser("~")) / "puff_state"
                            / "zone3_managed_devices.json")

    def test_whitespace_only_env_is_ignored(self, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "   ")
        assert pe._managed_devices_path() == (
            Path(pe._DEFAULT_DATA_DIR) / "zone3_managed_devices.json")

    @pytest.mark.parametrize("env_value", [None, "", "   ", "relstate",
                                           "~/puff_state", "<abs>"])
    def test_matches_the_canonical_resolver(self, env_value, tmp_path, monkeypatch):
        from puffpai import statedir
        if env_value == "<abs>":
            env_value = str(tmp_path / "state")
        if env_value is None:
            monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        else:
            monkeypatch.setenv("PUFFPAI_DATA_DIR", env_value)

        assert pe._managed_devices_path() == Path(resolve_state_dir(
            None, repo_root=statedir._REPO_ROOT,
            default=str(pe._DEFAULT_DATA_DIR))) / "zone3_managed_devices.json"

    def test_path_is_resolved_once_and_cached(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        first = pe._managed_devices_path()
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "moved"))
        assert pe._managed_devices_path() == first, "cached after first use"

    def test_engine_defaults_to_the_resolved_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        engine = pe.PolicyEngine(pihole=None)
        assert engine._managed_file == (
            tmp_path / "state" / "zone3_managed_devices.json")

    def test_explicit_managed_file_still_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        explicit = tmp_path / "explicit.json"
        assert pe.PolicyEngine(
            pihole=None, managed_file=str(explicit))._managed_file == explicit

    def test_ledger_is_written_to_the_relocated_state_dir(self, tmp_path, monkeypatch):
        """The ALWAYS_ALLOW marking actually survives to disk."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        engine = pe.PolicyEngine(pihole=None)
        engine._devices["10.0.0.9"] = pe.DevicePolicy(
            ip="10.0.0.9", mac="aa:bb:cc:dd:ee:ff", manufacturer="Acme Medical",
            hostname="pacemaker-hub", category="medical_device",
            mode=pe.SecurityMode.ALWAYS_ALLOW.value, is_safety_critical=True)

        engine._save_managed_devices()

        ledger = tmp_path / "state" / "zone3_managed_devices.json"
        assert ledger.is_file()
        rows = json.loads(ledger.read_text(encoding="utf-8"))
        assert rows[0]["ip"] == "10.0.0.9"
        assert rows[0]["mode"] == "always_allow"
