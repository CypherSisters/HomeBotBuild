"""HA device-watcher state-dir resolution — where the approval trail lands.

Three files carry the answer to "which HA entities did the user let onto this
network": the known-device list, the pending queue, and ha_device_approvals.jsonl.
They used to be import-time constants under the code tree, which a hardened
unit (ProtectSystem=strict) mounts read-only, so an append to the audit trail
raised and the approval went unrecorded. They now resolve through
puffpai.statedir, and these checks pin the two things that can silently go
wrong: the unset-env default must not move (an existing box's list is at the
old path), and a relative $PUFFPAI_DATA_DIR must join the REPO ROOT, not
whatever CWD the unit happened to start in.

No network: only the path helpers and the persistence methods are exercised.
The watcher's discovery path talks to Home Assistant and is never called here.
Every case writes into tmp_path — the real repo tree is never touched.

Run: py -3 -m pytest tests/test_ha_device_watcher_paths.py -q
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from puffpai.config import resolve_state_dir
from puffpai import ha_device_watcher as w


@pytest.fixture(autouse=True)
def _unresolved(monkeypatch):
    """Drop the cached dir and the singleton so each case re-resolves.

    The cache is what makes get_watcher()'s long-lived instance sticky; a test
    that changed the environment without this would silently assert against
    the previous case's directory.
    """
    monkeypatch.setattr(w, "_data_dir_cache", None)
    monkeypatch.setattr(w, "_instance", None)


_FILES = {
    "_known_devices_file": "ha_known_devices.json",
    "_pending_devices_file": "ha_pending_devices.json",
    "_approval_log_file": "ha_device_approvals.jsonl",
}


# ── defaults ─────────────────────────────────────────────────────────────

class TestUnsetEnvKeepsTheHistoricalPaths:
    def test_data_dir_is_the_repo_data_dir(self, monkeypatch):
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        assert w._data_dir() == w._REPO_ROOT / "data"

    @pytest.mark.parametrize("helper,filename", sorted(_FILES.items()))
    def test_each_file_keeps_its_historical_name_and_dir(
            self, helper, filename, monkeypatch):
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        assert getattr(w, helper)() == w._REPO_ROOT / "data" / filename

    def test_whitespace_only_env_is_ignored(self, monkeypatch):
        """A blank-but-set value must not relocate state to a junk directory."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "   ")
        assert w._data_dir() == w._REPO_ROOT / "data"


# ── relocation ───────────────────────────────────────────────────────────

class TestEnvRelocatesToTheStateRoot:
    @pytest.mark.parametrize("helper,filename", sorted(_FILES.items()))
    def test_files_land_flat_in_the_state_root(
            self, helper, filename, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        assert getattr(w, helper)() == tmp_path / "state" / filename

    def test_relative_env_joins_the_repo_root_not_the_cwd(self, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "relstate")
        assert w._data_dir() == w._REPO_ROOT / "relstate"

    def test_tilde_env_is_expanded(self, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "~/puff_state")
        resolved = w._data_dir()
        assert "~" not in str(resolved)
        assert resolved == Path(os.path.expanduser("~")) / "puff_state"

    @pytest.mark.parametrize("env_value", [None, "", "   ", "relstate",
                                           "~/puff_state", "<abs>"])
    def test_matches_the_canonical_resolver(self, env_value, tmp_path, monkeypatch):
        if env_value == "<abs>":
            env_value = str(tmp_path / "state")
        if env_value is None:
            monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        else:
            monkeypatch.setenv("PUFFPAI_DATA_DIR", env_value)

        assert w._data_dir() == Path(resolve_state_dir(
            None, repo_root=str(w._REPO_ROOT),
            default=str(w._REPO_ROOT / "data")))


# ── override hooks ───────────────────────────────────────────────────────

class TestOverrideHooks:
    """The four historical names stay usable as test/ops escape hatches.

    They also have to beat the cache: get_watcher() hands out one instance for
    the life of the process, so a hook that only won at first resolution would
    be unusable against a running appliance.

    They are plain None-default globals, deliberately not a PEP 562 module
    __getattr__ shim: a shim synthesises a resolved path on read, which
    monkeypatch.setattr records as the "old" value and re-assigns at teardown
    -- pinning a permanent override that silently disables $PUFFPAI_DATA_DIR
    for the rest of the process and sends the approval trail back into the
    repo tree. test_monkeypatch_setattr_does_not_outlive_the_test pins that.
    """

    @pytest.mark.parametrize("name", ["_DATA_DIR", "_KNOWN_DEVICES_FILE",
                                      "_PENDING_DEVICES_FILE",
                                      "_APPROVAL_LOG_FILE"])
    def test_the_hooks_default_to_none(self, name):
        """Unset means "resolve" -- the resolver is the source of truth."""
        assert getattr(w, name) is None

    def test_unknown_attribute_still_raises(self):
        with pytest.raises(AttributeError):
            w._NO_SUCH_PATH

    def test_data_dir_hook_wins_over_the_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "env"))
        monkeypatch.setattr(w, "_DATA_DIR", tmp_path / "pinned")

        assert w._data_dir() == tmp_path / "pinned"
        assert w._known_devices_file() == (
            tmp_path / "pinned" / "ha_known_devices.json")

    def test_hook_accepts_a_plain_string(self, tmp_path, monkeypatch):
        monkeypatch.setattr(w, "_DATA_DIR", str(tmp_path / "pinned"))
        assert w._data_dir() == tmp_path / "pinned"

    @pytest.mark.parametrize("name,helper", [
        ("_KNOWN_DEVICES_FILE", "_known_devices_file"),
        ("_PENDING_DEVICES_FILE", "_pending_devices_file"),
        ("_APPROVAL_LOG_FILE", "_approval_log_file"),
    ])
    def test_per_file_hook_wins_over_the_env(
            self, name, helper, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "env"))
        pinned = tmp_path / f"pinned_{name}.json"
        monkeypatch.setattr(w, name, pinned)

        assert getattr(w, helper)() == pinned

    def test_hook_beats_an_already_resolved_cache(self, tmp_path, monkeypatch):
        """The sting of the singleton: redirect must work mid-life."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "env"))
        assert w._data_dir() == tmp_path / "env"          # cache now warm

        monkeypatch.setattr(w, "_DATA_DIR", tmp_path / "pinned")
        assert w._data_dir() == tmp_path / "pinned"

    def test_monkeypatch_setattr_does_not_outlive_the_test(
            self, tmp_path, monkeypatch):
        """The reason these are plain globals rather than a __getattr__ shim."""
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(w, "_DATA_DIR", tmp_path / "pinned")
            assert w._data_dir() == tmp_path / "pinned"

        assert w._DATA_DIR is None
        assert w._data_dir() == w._REPO_ROOT / "data"

    def test_mock_patch_also_works(self, tmp_path, monkeypatch):
        """The other idiom the existing suite uses."""
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        with patch.object(w, "_DATA_DIR", tmp_path / "pinned"):
            assert w._data_dir() == tmp_path / "pinned"

        assert w._DATA_DIR is None

    def test_a_non_path_hook_is_ignored(self, monkeypatch):
        """A MagicMock answers __fspath__ with junk; resolve around it."""
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        monkeypatch.setattr(w, "_DATA_DIR", MagicMock())

        assert w._data_dir() == w._REPO_ROOT / "data"


# ── functional writes ────────────────────────────────────────────────────

class TestStateActuallyLandsInTheStateDir:
    """Not just the path -- the bytes.

    The watcher is constructed directly rather than through get_watcher() so
    no singleton leaks into the rest of the session, and nothing here calls
    check_for_new_devices(), which is the only method that talks to HA.
    """

    def test_known_and_pending_are_written_to_a_state_dir_that_does_not_exist_yet(
            self, tmp_path, monkeypatch):
        state = tmp_path / "state" / "nested"
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(state))
        assert not state.exists()

        watcher = w.HADeviceWatcher()
        watcher._known["light.kitchen"] = w.KnownDevice(
            entity_id="light.kitchen", domain="light",
            friendly_name="Kitchen", status="approved",
            security_mode="lockdown", approved_at="2026-01-01T00:00:00")
        watcher._pending["lock.front"] = w.PendingDevice(
            entity_id="lock.front", domain="lock",
            friendly_name="Front Door", state="locked")
        watcher._save_known()
        watcher._save_pending()

        known = state / "ha_known_devices.json"
        pending = state / "ha_pending_devices.json"
        assert known.is_file() and pending.is_file()
        assert json.loads(known.read_text())["light.kitchen"]["status"] == "approved"
        assert json.loads(pending.read_text())["lock.front"]["domain"] == "lock"

    def test_approval_log_appends_to_the_relocated_trail(self, tmp_path, monkeypatch):
        """The audit trail is the record of what the user let in."""
        state = tmp_path / "state"
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(state))
        watcher = w.HADeviceWatcher()

        watcher._log_approval("lock.front", "approved", {"security_mode": "lockdown"})
        watcher._log_approval("camera.porch", "rejected", {"firewall_blocked": True})

        trail = state / "ha_device_approvals.jsonl"
        rows = [json.loads(line) for line in
                trail.read_text(encoding="utf-8").splitlines()]
        assert [r["entity_id"] for r in rows] == ["lock.front", "camera.porch"]
        assert rows[0]["action"] == "approved"

    def test_a_per_file_hook_creates_its_own_parent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        pinned = tmp_path / "elsewhere" / "deep" / "approvals.jsonl"
        monkeypatch.setattr(w, "_APPROVAL_LOG_FILE", pinned)

        w.HADeviceWatcher()._log_approval("lock.front", "approved", {})

        assert pinned.is_file()

    def test_nothing_is_written_to_the_repo_tree(self, tmp_path, monkeypatch):
        """Guard for these tests themselves, not just for the product."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        repo_known = w._REPO_ROOT / "data" / "ha_known_devices.json"
        before = repo_known.exists()
        before_bytes = repo_known.read_bytes() if before else None

        watcher = w.HADeviceWatcher()
        watcher._save_known()
        watcher._log_approval("lock.front", "approved", {})

        assert repo_known.exists() is before
        if before:
            assert repo_known.read_bytes() == before_bytes
