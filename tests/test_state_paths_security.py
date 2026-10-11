"""Where the HA device watcher keeps its state, and who else reads it.

``ha_device_watcher`` has no per-module test file, so its state-dir migration
onto :mod:`puffpai.statedir` is covered here. Three files matter:

* ``ha_known_devices.json`` / ``ha_pending_devices.json`` — the approval queue.
  If these cannot be written, every restart re-presents devices the user
  already decided about, and "approved" silently stops meaning anything.
* ``ha_device_approvals.jsonl`` — the audit trail of which HA entities the
  user actually let in. This is the security-relevant one: it is the only
  record of those decisions.

The last file is also read by ``security/consent_gate`` as its
``LEGACY_APPROVALS``. The two modules must not derive its directory from
different defaults; that convergence is
pinned below, with the env var set AND unset.

Run: pytest tests/test_state_paths_security.py -q
"""

import importlib
import json
import os
from pathlib import Path

import pytest

from puffpai import ha_device_watcher as hw
from puffpai import statedir
from puffpai.config import resolve_state_dir


@pytest.fixture(autouse=True)
def _unresolved(monkeypatch):
    """Drop the cached directory so each case re-resolves from the env."""
    monkeypatch.setattr(hw, "_data_dir_cache", None)


def _canonical_dir() -> Path:
    return Path(resolve_state_dir(
        None, repo_root=statedir._REPO_ROOT, default=str(hw._DEFAULT_DATA_DIR)))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestWatcherStateDir:
    def test_unset_env_keeps_the_historical_location(self, monkeypatch):
        """Byte-identical to the pre-migration constants.

        Those were ``Path(__file__).resolve().parent.parent.parent / "data"``
        from src/puffpai/ha_device_watcher.py — three parents up IS the repo
        root here (unlike zone3/policy_engine.py, one package deeper, whose
        same-shaped expression pointed at <repo>/src). Recomputed literally
        rather than read off the module, so a change to _DEFAULT_DATA_DIR
        cannot make this test agree with itself.
        """
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        old_data_dir = (Path(hw.__file__).resolve().parent.parent.parent
                        / "data")

        assert hw._data_dir() == old_data_dir
        assert hw._known_devices_file() == old_data_dir / "ha_known_devices.json"
        assert hw._pending_devices_file() == old_data_dir / "ha_pending_devices.json"
        assert hw._approval_log_file() == old_data_dir / "ha_device_approvals.jsonl"

    def test_env_set_puts_all_three_files_in_the_state_dir(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        state = tmp_path / "state"

        assert hw._known_devices_file() == state / "ha_known_devices.json"
        assert hw._pending_devices_file() == state / "ha_pending_devices.json"
        assert hw._approval_log_file() == state / "ha_device_approvals.jsonl"

    def test_relative_env_joins_the_repo_root_not_the_cwd(self, monkeypatch):
        """A unit started from / must not scatter state into the CWD."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "relstate")
        assert hw._data_dir() == Path(statedir._REPO_ROOT) / "relstate"

    def test_tilde_env_is_expanded(self, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "~/puff_state")
        resolved = hw._data_dir()
        assert "~" not in str(resolved)
        assert resolved == Path(os.path.expanduser("~")) / "puff_state"

    def test_whitespace_only_env_is_ignored(self, monkeypatch):
        """A blank-but-set value must not relocate state to a junk directory."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "   ")
        assert hw._data_dir() == Path(hw._DEFAULT_DATA_DIR)

    @pytest.mark.parametrize("env_value", [None, "", "   ", "relstate",
                                           "~/puff_state", "../sibling", "<abs>"])
    def test_matches_the_canonical_resolver(self, env_value, tmp_path, monkeypatch):
        if env_value == "<abs>":
            env_value = str(tmp_path / "state")
        if env_value is None:
            monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        else:
            monkeypatch.setenv("PUFFPAI_DATA_DIR", env_value)

        assert hw._data_dir() == _canonical_dir()

    def test_path_is_resolved_once_and_cached(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        first = hw._data_dir()
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "moved"))
        assert hw._data_dir() == first, "cached after first use"

    def test_reload_resets_the_cache(self, monkeypatch):
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        hw._data_dir()
        reloaded = importlib.reload(hw)
        try:
            assert reloaded._data_dir_cache is None
        finally:
            importlib.reload(hw)

    def test_module_import_does_not_resolve_anything(self, monkeypatch):
        """The whole point of the lazy accessor: importing must not touch
        config (which reaches the secrets provider) or the environment."""
        reloaded = importlib.reload(hw)
        try:
            assert reloaded._data_dir_cache is None
        finally:
            importlib.reload(hw)


# ---------------------------------------------------------------------------
# The bytes actually land there
# ---------------------------------------------------------------------------


class TestWatcherStateIsWrittenToTheResolvedDir:
    def test_approval_trail_is_appended_in_the_state_dir(
            self, tmp_path, monkeypatch):
        """The audit entry is the record of what the user let onto the network."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        watcher = hw.HADeviceWatcher()

        watcher._log_approval("light.porch", "approved", {"lockdown": True})

        trail = tmp_path / "state" / "ha_device_approvals.jsonl"
        assert trail.is_file()
        row = json.loads(trail.read_text(encoding="utf-8").splitlines()[0])
        assert row["entity_id"] == "light.porch"
        assert row["action"] == "approved"

    def test_known_and_pending_round_trip_through_the_state_dir(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        watcher = hw.HADeviceWatcher()
        watcher._known["light.porch"] = hw.KnownDevice(
            entity_id="light.porch", friendly_name="Porch", domain="light",
            status="approved")

        watcher._save_known()

        assert (tmp_path / "state" / "ha_known_devices.json").is_file()
        monkeypatch.setattr(hw, "_data_dir_cache", None)
        assert "light.porch" in hw.HADeviceWatcher()._known


# ---------------------------------------------------------------------------
# Coupling: consent_gate reads the watcher's approval trail
# ---------------------------------------------------------------------------
#
# security/consent_gate exposes the same filename as LEGACY_APPROVALS: the
# watcher WRITES that trail, the gate READS it to decide whether an entity is
# merely "legacy_blanket" (flag) or "pending" (hard deny). If the gate
# derived it from ITS default, <repo>/src/puffpai/data, while the watcher
# derived it from <repo>/data, then with the env var unset the reader would
# read a file nobody wrote and every blanket-approved entity would silently
# become a deny.
# The gate's default is <repo>/data; its own old directory
# survives as a file-level legacy fallback, so no box's state is orphaned.
# These tests pin the writer path == reader path in BOTH env states, because
# that equality is the whole security property.


class TestApprovalTrailCoupling:
    @staticmethod
    def _consent_gate(monkeypatch):
        from puffpai.security import consent_gate as cg
        monkeypatch.setattr(cg, "_data_dir_cache", None)
        return cg

    def test_env_unset_the_two_modules_converge(self, tmp_path, monkeypatch):
        """The default-path half of the coupling: one file, not two.

        Hermetic against whatever a real box has lying around: the legacy
        directory is redirected at an empty tmp dir so file-level adoption
        cannot fire, which is a state-dependent answer, not the default this
        pins. The autouse fixture only resets the watcher's cache, so the
        gate's is reset here too.
        """
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        cg = self._consent_gate(monkeypatch)
        monkeypatch.setattr(cg, "_LEGACY_DATA_DIR", str(tmp_path / "empty"))

        watcher_trail = hw._approval_log_file()
        gate_trail = Path(cg.LEGACY_APPROVALS)

        assert watcher_trail.name == gate_trail.name == "ha_device_approvals.jsonl"
        assert watcher_trail == gate_trail, (
            "the gate reads what the watcher writes -- if these diverge "
            "again, blanket-approved entities silently become hard denies")
        assert gate_trail == (Path(statedir._REPO_ROOT) / "data"
                              / "ha_device_approvals.jsonl")

    def test_env_set_makes_them_converge_on_one_file(self, tmp_path, monkeypatch):
        """The deployment configuration that resolves the split."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        cg = self._consent_gate(monkeypatch)

        assert hw._approval_log_file() == Path(cg.LEGACY_APPROVALS)
        assert hw._approval_log_file() == (
            tmp_path / "state" / "ha_device_approvals.jsonl")

    def test_consent_gate_reads_what_the_watcher_wrote(self, tmp_path, monkeypatch):
        """Convergence at the byte level, not just the path level."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        cg = self._consent_gate(monkeypatch)
        hw.HADeviceWatcher()._log_approval(
            "camera.porch", "approved", {"lockdown": False})

        rows = [json.loads(line) for line
                in Path(cg.LEGACY_APPROVALS).read_text(
                    encoding="utf-8").splitlines() if line.strip()]

        assert [r["entity_id"] for r in rows] == ["camera.porch"]


# ---------------------------------------------------------------------------
# The gate adopts the trail its own old default left behind
# ---------------------------------------------------------------------------
#
# Moving the gate's default onto <repo>/data would orphan a box that has been
# running long enough to hold state under <repo>/src/puffpai/data. Adoption is
# FILE-level, not the directory-level ``legacy=`` of the canon, because
# <repo>/data exists in every checkout and a directory probe would never fall
# through. Every case below redirects both constants at tmp_path, so nothing
# depends on what this machine happens to have in either real directory.


class TestApprovalTrailLegacyAdoption:
    @staticmethod
    def _redirect(monkeypatch, tmp_path):
        """Point the gate at tmp new/old dirs and drop its cached dir.

        ``new`` is created empty on purpose: <repo>/data exists in every
        checkout, so leaving it absent would let the canon's directory-level
        ``legacy=`` answer instead, and these cases would never reach the
        file-level rule they exist to pin.
        """
        from puffpai.security import consent_gate as cg
        (tmp_path / "new").mkdir(exist_ok=True)
        monkeypatch.setattr(cg, "_DEFAULT_DATA_DIR", str(tmp_path / "new"))
        monkeypatch.setattr(cg, "_LEGACY_DATA_DIR", str(tmp_path / "old"))
        monkeypatch.setattr(cg, "_data_dir_cache", None)
        return cg

    @staticmethod
    def _seed(directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        trail = directory / "ha_device_approvals.jsonl"
        trail.write_text(
            json.dumps({"entity_id": "light.porch", "action": "approved"})
            + "\n", encoding="utf-8")
        return trail

    def test_legacy_trail_is_adopted_when_the_new_path_has_none(
            self, tmp_path, monkeypatch):
        """An upgraded box must not lose the record of what the user let in.

        Without adoption those entities read back as "pending" -- a hard deny
        -- rather than "legacy_blanket", which flags. That is a behaviour
        change on every automation targeting them, not just a lost file.
        """
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        cg = self._redirect(monkeypatch, tmp_path)
        legacy = self._seed(tmp_path / "old")

        assert Path(cg.LEGACY_APPROVALS) == legacy

    def test_a_trail_at_the_new_path_wins(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        cg = self._redirect(monkeypatch, tmp_path)
        self._seed(tmp_path / "old")
        current = self._seed(tmp_path / "new")

        assert Path(cg.LEGACY_APPROVALS) == current

    def test_legacy_never_overrides_an_explicit_state_dir(
            self, tmp_path, monkeypatch):
        """$PUFFPAI_DATA_DIR is an operator's explicit choice; a stale file in
        the code tree must not quietly win over it."""
        cg = self._redirect(monkeypatch, tmp_path)
        legacy = self._seed(tmp_path / "old")
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))

        resolved = Path(cg.LEGACY_APPROVALS)
        assert resolved == tmp_path / "state" / "ha_device_approvals.jsonl"
        assert resolved != legacy

    def test_env_pointed_at_the_default_dir_still_suppresses_adoption(
            self, tmp_path, monkeypatch, caplog):
        """The env var being SET is what suppresses adoption -- not the dir
        having moved.

        Deciding by dir comparison reads an operator who sets the var to the
        default dir as "unset", adopts, and the gate then reads AND WRITES in
        the read-only code tree while an env var says otherwise.
        """
        cg = self._redirect(monkeypatch, tmp_path)
        legacy = self._seed(tmp_path / "old")
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "new"))

        with caplog.at_level("WARNING"):
            resolved = Path(cg.LEGACY_APPROVALS)
        assert resolved == tmp_path / "new" / "ha_device_approvals.jsonl"
        assert resolved != legacy
        assert "legacy" not in caplog.text

    @pytest.mark.parametrize("env_value", ["", "   "])
    def test_a_blank_env_var_is_unset_for_adoption_too(
            self, env_value, tmp_path, monkeypatch):
        """Canon treats empty/whitespace as unset; adoption must agree."""
        cg = self._redirect(monkeypatch, tmp_path)
        legacy = self._seed(tmp_path / "old")
        monkeypatch.setenv("PUFFPAI_DATA_DIR", env_value)

        assert Path(cg.LEGACY_APPROVALS) == legacy

    def test_the_gate_reads_the_adopted_trail(self, tmp_path, monkeypatch):
        """Adoption at the byte level: the blanket entity flags, not denies."""
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        cg = self._redirect(monkeypatch, tmp_path)
        self._seed(tmp_path / "old")
        monkeypatch.setattr(cg.ConsentStore, "_legacy_cache", None)

        assert cg.ConsentStore().status("light.porch") == "legacy_blanket"
