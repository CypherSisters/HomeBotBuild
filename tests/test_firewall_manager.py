"""Tests for firewall_manager.py -- backend abstraction + security."""

import json
import sys
import pytest
from unittest.mock import patch, MagicMock

from puffpai.firewall_manager import (
    FirewallManager, FirewallBackend, WindowsFirewall, NftablesFirewall,
    MockFirewall, UnavailableFirewall, get_firewall_backend,
    validate_ip, _sanitize_rule_name, _validate_reason,
)


# ── IP validation ────────────────────────────────────────────────────────

class TestValidateIP:
    """Test IP validation -- the core security fix."""

    def test_valid_ipv4(self):
        addr = validate_ip("192.168.1.100")
        assert str(addr) == "192.168.1.100"

    def test_valid_ipv4_with_whitespace(self):
        addr = validate_ip("  10.0.0.1  ")
        assert str(addr) == "10.0.0.1"

    def test_rejects_loopback(self):
        with pytest.raises(ValueError, match="loopback"):
            validate_ip("127.0.0.1")

    def test_rejects_multicast(self):
        with pytest.raises(ValueError, match="multicast"):
            validate_ip("224.0.0.1")

    def test_rejects_link_local(self):
        with pytest.raises(ValueError, match="link-local"):
            validate_ip("169.254.1.1")

    def test_rejects_broadcast(self):
        with pytest.raises(ValueError, match="broadcast"):
            validate_ip("255.255.255.255")

    def test_rejects_unspecified(self):
        with pytest.raises(ValueError, match="unspecified"):
            validate_ip("0.0.0.0")

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="empty"):
            validate_ip("")

    def test_rejects_shell_injection_semicolon(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_ip("192.168.1.1; rm -rf /")

    def test_rejects_shell_injection_ampersand(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_ip("192.168.1.1 & del /f /q C:\\*")

    def test_rejects_shell_injection_pipe(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_ip("192.168.1.1 | whoami")

    def test_rejects_shell_injection_backtick(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_ip("`whoami`")

    def test_rejects_garbage(self):
        with pytest.raises(ValueError, match="Invalid IP"):
            validate_ip("not-an-ip")


class TestSanitizeRuleName:
    def test_normal_ip(self):
        assert _sanitize_rule_name("192.168.1.100") == "PuffPAI_Block_192_168_1_100"

    def test_ipv6_colons(self):
        name = _sanitize_rule_name("::1")
        assert "PuffPAI_Block_" in name
        assert ";" not in name


class TestValidateReason:
    def test_normal_reason(self):
        assert _validate_reason("Suspicious activity") == "Suspicious activity"

    def test_truncates_long(self):
        long = "x" * 300
        assert len(_validate_reason(long)) <= 200

    def test_strips_control_chars(self):
        assert _validate_reason("test\x00\x01\x02end") == "testend"

    def test_empty_fallback(self):
        assert _validate_reason("") == "User requested"


# ── WindowsFirewall (backwards-compat FirewallManager) ────────────────────

class TestFirewallManager:
    """Tests for WindowsFirewall via the FirewallManager alias."""

    @patch('puffpai.firewall_manager._BLOCKED_FILE')
    def test_block_invalid_ip(self, mock_file, tmp_path):
        blocked_file = tmp_path / "blocked.json"
        blocked_file.write_text("[]")
        with patch('puffpai.firewall_manager._BLOCKED_FILE', str(blocked_file)):
            fw = FirewallManager()
            result = fw.block_device("127.0.0.1")
            assert result['success'] is False
            assert 'loopback' in result['message'].lower()

    @patch('subprocess.run')
    def test_block_valid_ip(self, mock_run, tmp_path):
        blocked_file = tmp_path / "blocked.json"
        blocked_file.write_text("[]")
        mock_run.return_value = MagicMock(stdout="Ok.", returncode=0)

        with patch('puffpai.firewall_manager._BLOCKED_FILE', str(blocked_file)):
            fw = FirewallManager()
            result = fw.block_device("192.168.1.100")
            assert result['success'] is True
            assert '192.168.1.100' in result['message']

        # Verify shell=False was used
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get('shell') is False or call_kwargs[1].get('shell') is False

    @patch('subprocess.run')
    def test_block_duplicate(self, mock_run, tmp_path):
        blocked_file = tmp_path / "blocked.json"
        blocked_file.write_text(json.dumps([{"ip": "192.168.1.100"}]))

        with patch('puffpai.firewall_manager._BLOCKED_FILE', str(blocked_file)):
            fw = FirewallManager()
            result = fw.block_device("192.168.1.100")
            assert result['success'] is False
            assert 'already blocked' in result['message']

    def test_get_status(self, tmp_path):
        blocked_file = tmp_path / "blocked.json"
        blocked_file.write_text(json.dumps([
            {"ip": "192.168.1.50", "rule_name": "PuffPAI_Block_192_168_1_50"}
        ]))
        with patch('puffpai.firewall_manager._BLOCKED_FILE', str(blocked_file)):
            fw = FirewallManager()
            status = fw.get_status()
            assert status['blocked_count'] == 1


# ── FirewallManager is WindowsFirewall ────────────────────────────────────

class TestBackwardsCompat:
    def test_firewall_manager_is_windows(self):
        assert FirewallManager is WindowsFirewall

    def test_firewall_manager_is_backend_subclass(self):
        assert issubclass(FirewallManager, FirewallBackend)


# ── MockFirewall ──────────────────────────────────────────────────────────

class TestMockFirewall:
    def test_block_and_unblock(self, tmp_path):
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        fw = MockFirewall(str(bf))

        result = fw.block_device("192.168.1.50")
        assert result['success'] is True
        assert "192.168.1.50" in fw._mock_rules
        assert fw.get_status()['blocked_count'] == 1

        result = fw.unblock_device("192.168.1.50")
        assert result['success'] is True
        assert "192.168.1.50" not in fw._mock_rules
        assert fw.get_status()['blocked_count'] == 0

    def test_lockdown(self, tmp_path):
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        fw = MockFirewall(str(bf))

        result = fw.lockdown_mode(True)
        assert result['success'] is True
        assert fw._lockdown is True

        result = fw.lockdown_mode(False)
        assert result['success'] is True
        assert fw._lockdown is False

    def test_is_available(self, tmp_path):
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        assert MockFirewall(str(bf), available=True).is_available() is True
        assert MockFirewall(str(bf), available=False).is_available() is False

    def test_status_includes_backend(self, tmp_path):
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        fw = MockFirewall(str(bf))
        assert fw.get_status()['backend'] == 'MockFirewall'

    def test_duplicate_block(self, tmp_path):
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        fw = MockFirewall(str(bf))
        fw.block_device("192.168.1.50")
        result = fw.block_device("192.168.1.50")
        assert result['success'] is False
        assert 'already blocked' in result['message']

    def test_max_devices(self, tmp_path):
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        fw = MockFirewall(str(bf))
        # Fill with 500 fake entries
        fw.blocked_devices = [{"ip": f"10.0.{i//256}.{i%256}"} for i in range(500)]
        result = fw.block_device("192.168.1.50")
        assert result['success'] is False
        assert 'Max' in result['message']

    def test_invalid_ip_rejected(self, tmp_path):
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        fw = MockFirewall(str(bf))
        result = fw.block_device("127.0.0.1")
        assert result['success'] is False

    def test_persists_to_file(self, tmp_path):
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        fw = MockFirewall(str(bf))
        fw.block_device("192.168.1.50")

        # Verify file was written
        data = json.loads(bf.read_text())
        assert len(data) == 1
        assert data[0]['ip'] == '192.168.1.50'


# ── NftablesFirewall ──────────────────────────────────────────────────────

class TestNftablesFirewall:
    @patch('subprocess.run')
    def test_block_builds_correct_nft_command(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        fw = NftablesFirewall(str(bf))

        result = fw.block_device("192.168.1.50")
        assert result['success'] is True

        # Verify nft add element was called
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == 'nft'
        assert 'add' in call_args
        assert 'element' in call_args
        assert '192.168.1.50' in call_args

    @patch('subprocess.run')
    def test_unblock_builds_correct_nft_command(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        bf = tmp_path / "blocked.json"
        bf.write_text(json.dumps([{"ip": "192.168.1.50", "rule_name": "PuffPAI_Block_192_168_1_50"}]))
        fw = NftablesFirewall(str(bf))

        result = fw.unblock_device("192.168.1.50")
        assert result['success'] is True

        call_args = mock_run.call_args[0][0]
        assert 'delete' in call_args
        assert '192.168.1.50' in call_args

    @patch('subprocess.run')
    def test_lockdown_sets_drop_policy(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        fw = NftablesFirewall(str(bf))

        result = fw.lockdown_mode(True)
        assert result['success'] is True
        call_args = mock_run.call_args[0][0]
        assert 'drop' in call_args

    @patch('subprocess.run')
    def test_lockdown_disable_sets_accept(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        fw = NftablesFirewall(str(bf))

        result = fw.lockdown_mode(False)
        assert result['success'] is True
        call_args = mock_run.call_args[0][0]
        assert 'accept' in call_args

    @patch('subprocess.run')
    def test_nft_not_found(self, mock_run, tmp_path):
        mock_run.side_effect = FileNotFoundError
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        fw = NftablesFirewall(str(bf))

        result = fw.block_device("192.168.1.50")
        assert result['success'] is False
        assert 'nft not found' in result['message']

    @patch('subprocess.run')
    def test_ensure_table(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        fw = NftablesFirewall(str(bf))

        result = fw.ensure_table()
        assert result['success'] is True
        # Should have called nft multiple times (table + sets + chain + rules)
        assert mock_run.call_count >= 4


# ── Backend factory ───────────────────────────────────────────────────────

class TestGetFirewallBackend:
    def test_windows_returns_windows(self, tmp_path):
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        with patch.object(sys, 'platform', 'win32'):
            fw = get_firewall_backend(str(bf))
            assert isinstance(fw, WindowsFirewall)

    def test_linux_returns_nftables_when_opted_in(self, tmp_path, monkeypatch):
        # nftables is not chosen by platform guess. A host
        # that can genuinely enforce rules opts in; see get_firewall_backend.
        monkeypatch.setenv("PUFFPAI_FIREWALL_BACKEND", "nftables")
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        with patch.object(sys, 'platform', 'linux'):
            fw = get_firewall_backend(str(bf))
            assert isinstance(fw, NftablesFirewall)

    def test_linux_defaults_to_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PUFFPAI_FIREWALL_BACKEND", raising=False)
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        with patch.object(sys, 'platform', 'linux'):
            fw = get_firewall_backend(str(bf))
            assert isinstance(fw, UnavailableFirewall)

    def test_unknown_returns_unavailable_not_mock(self, tmp_path, monkeypatch):
        # MockFirewall reports success for a block that never happened, which
        # is the one answer a security control must never give in production.
        monkeypatch.delenv("PUFFPAI_FIREWALL_BACKEND", raising=False)
        bf = tmp_path / "blocked.json"
        bf.write_text("[]")
        with patch.object(sys, 'platform', 'darwin'):
            fw = get_firewall_backend(str(bf))
            assert isinstance(fw, UnavailableFirewall)
            assert not isinstance(fw, MockFirewall)

    def test_all_backends_are_firewall_backend(self):
        assert issubclass(WindowsFirewall, FirewallBackend)
        assert issubclass(NftablesFirewall, FirewallBackend)
        assert issubclass(MockFirewall, FirewallBackend)


# ── state-dir resolution ─────────────────────────────────────────────────
#
# The block list and the rule file are what the firewall rebuilds itself from
# after a restart. They used to be import-time constants under the code tree,
# which a hardened unit mounts read-only -- a device the user had just blocked
# came back unblocked. They now resolve through puffpai.statedir, so these
# checks pin the two things that can silently go wrong: the unset-env default
# must not move (an existing box's block list is at the old path), and a
# relative $PUFFPAI_DATA_DIR must join the REPO ROOT, not whatever CWD the
# unit happened to start in.

import os                                              # noqa: E402
import importlib                                       # noqa: E402
from puffpai import firewall_manager as fw_mod         # noqa: E402
from puffpai.config import resolve_state_dir           # noqa: E402


@pytest.fixture(autouse=True)
def _unresolved_fw_paths(monkeypatch):
    """Drop the cached paths so each case re-resolves from the env."""
    monkeypatch.setattr(fw_mod, "_blocked_file_cache", None)
    monkeypatch.setattr(fw_mod, "_rules_file_cache", None)


class TestStateDirResolution:
    def test_unset_env_keeps_the_historical_locations(self, monkeypatch):
        """Byte-identical to the pre-migration constants.

        The block list sits at the REPO ROOT, not under data/ -- an odd split,
        but moving it would orphan an existing box's list, so it is preserved
        deliberately.
        """
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        assert fw_mod._blocked_file() == os.path.join(
            fw_mod._REPO_ROOT, "blocked_devices.json")
        assert fw_mod._rules_file() == os.path.join(
            fw_mod._REPO_ROOT, "data", "firewall_rules.json")

    def test_env_set_puts_both_files_in_the_state_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        assert fw_mod._blocked_file() == str(tmp_path / "state" / "blocked_devices.json")
        assert fw_mod._rules_file() == str(tmp_path / "state" / "firewall_rules.json")

    def test_relative_env_joins_the_repo_root_not_the_cwd(self, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "relstate")
        assert fw_mod._blocked_file() == os.path.join(
            fw_mod._REPO_ROOT, "relstate", "blocked_devices.json")

    def test_tilde_env_is_expanded(self, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "~/puff_state")
        resolved = fw_mod._blocked_file()
        assert "~" not in resolved
        assert resolved == os.path.join(
            os.path.expanduser("~"), "puff_state", "blocked_devices.json")

    def test_whitespace_only_env_is_ignored(self, monkeypatch):
        """A blank-but-set value must not relocate state to a junk directory."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "   ")
        assert fw_mod._blocked_file() == os.path.join(
            fw_mod._REPO_ROOT, "blocked_devices.json")

    @pytest.mark.parametrize("env_value", [None, "", "   ", "relstate",
                                           "~/puff_state", "<abs>"])
    def test_matches_the_canonical_resolver(self, env_value, tmp_path, monkeypatch):
        if env_value == "<abs>":
            env_value = str(tmp_path / "state")
        if env_value is None:
            monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        else:
            monkeypatch.setenv("PUFFPAI_DATA_DIR", env_value)

        assert fw_mod._blocked_file() == os.path.join(
            resolve_state_dir(None, repo_root=fw_mod._REPO_ROOT,
                              default=fw_mod._REPO_ROOT),
            "blocked_devices.json")
        assert fw_mod._rules_file() == os.path.join(
            resolve_state_dir(None, repo_root=fw_mod._REPO_ROOT,
                              default=os.path.join(fw_mod._REPO_ROOT, "data")),
            "firewall_rules.json")

    def test_path_is_resolved_once_and_cached(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        first = fw_mod._blocked_file()
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "moved"))
        assert fw_mod._blocked_file() == first, "cached after first use"

    def test_reload_resets_the_cache(self, monkeypatch):
        """importlib.reload is the documented way to re-resolve."""
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        fw_mod._blocked_file()
        reloaded = importlib.reload(fw_mod)
        try:
            assert reloaded._blocked_file_cache is None
        finally:
            importlib.reload(fw_mod)


class TestPathAttributeBackCompat:
    """``_BLOCKED_FILE`` / ``_RULES_FILE`` still patch as attributes.

    Four tests in this file patch _BLOCKED_FILE, and they must keep working
    untouched. The names are plain None-default globals, not a PEP 562
    __getattr__ shim: a shim synthesises a resolved path on read, which
    monkeypatch.setattr records as the "old" value and re-assigns at teardown,
    silently pinning a permanent override.
    """

    def test_the_hooks_default_to_none(self, monkeypatch, tmp_path):
        """Unset means "resolve"; the resolver, not the hook, is the answer."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        assert fw_mod._BLOCKED_FILE is None
        assert fw_mod._RULES_FILE is None
        assert fw_mod._blocked_file() == str(tmp_path / "state" / "blocked_devices.json")

    def test_monkeypatch_setattr_does_not_outlive_the_test(self, tmp_path, monkeypatch):
        """The reason these are plain globals rather than a __getattr__ shim.

        Against a shim, monkeypatch would restore the resolved path it read at
        setup and $PUFFPAI_DATA_DIR would stop being honoured from here on.
        """
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(fw_mod, "_BLOCKED_FILE", str(tmp_path / "pinned.json"))
            assert fw_mod._blocked_file() == str(tmp_path / "pinned.json")

        assert fw_mod._BLOCKED_FILE is None
        assert fw_mod._blocked_file() == os.path.join(
            fw_mod._REPO_ROOT, "blocked_devices.json")

    def test_patching_the_attribute_overrides_the_resolver(self, tmp_path):
        override = str(tmp_path / "patched.json")
        with patch("puffpai.firewall_manager._BLOCKED_FILE", override):
            assert fw_mod._blocked_file() == override
            assert fw_mod._BLOCKED_FILE == override

    def test_the_override_is_removed_again_afterwards(self, tmp_path, monkeypatch):
        """The resolver must take over again rather than keep a stale pin."""
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        with patch("puffpai.firewall_manager._BLOCKED_FILE", str(tmp_path / "x.json")):
            pass
        assert fw_mod._BLOCKED_FILE is None
        assert fw_mod._blocked_file() == os.path.join(
            fw_mod._REPO_ROOT, "blocked_devices.json")

    def test_backend_uses_the_patched_path(self, tmp_path):
        """The end the tests above actually care about: the backend writes
        where the patch says, not where the env says."""
        blocked = tmp_path / "patched.json"
        blocked.write_text("[]")
        with patch("puffpai.firewall_manager._BLOCKED_FILE", str(blocked)):
            assert MockFirewall()._blocked_file == str(blocked)

    def test_unknown_attribute_still_raises(self):
        with pytest.raises(AttributeError):
            fw_mod._NO_SUCH_PATH


class TestRulesFileFollowsTheStateDir:
    def test_rules_are_written_to_the_relocated_state_dir(self, tmp_path, monkeypatch):
        """Not just the path -- the bytes land there."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        backend = MockFirewall(str(tmp_path / "blocked.json"))

        backend.add_rule("allow_ssh", "inbound", "allow", "TCP", "22")

        rules_file = tmp_path / "state" / "firewall_rules.json"
        assert rules_file.is_file()
        assert json.loads(rules_file.read_text())[0]["name"] == "allow_ssh"


# ── construction must stay inert ─────────────────────────────────────────
#
# api_server.py builds a FirewallManager at MODULE SCOPE. Resolving a state
# path imports puffpai.config, which reaches the secrets provider -- doing that
# from a constructor means importing the API server drags the vault in
# mid-import, the cycle puffpai.statedir exists to avoid. The constructor is
# therefore inert and everything resolves on first real use; these tests pin
# that, because the regression is invisible until an import cycle bites.

import sys                                              # noqa: E402
from pathlib import Path                                # noqa: E402


class TestConstructionDoesNotImportConfig:
    def test_constructor_does_not_import_config(self, monkeypatch):
        """Drop config from sys.modules; constructing must not bring it back."""
        monkeypatch.delitem(sys.modules, "puffpai.config", raising=False)

        FirewallManager()

        assert "puffpai.config" not in sys.modules

    def test_first_operation_does_import_config(self, monkeypatch, tmp_path):
        """The other half: resolution is deferred, not skipped."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        monkeypatch.delitem(sys.modules, "puffpai.config", raising=False)
        backend = MockFirewall()

        backend.get_status()

        assert "puffpai.config" in sys.modules

    def test_constructor_reads_nothing_from_disk(self, tmp_path):
        backend = MockFirewall(str(tmp_path / "blocked.json"))
        assert backend._blocked_devices is None, "loaded at construction"

    def test_list_is_read_on_first_access_not_at_construction(self, tmp_path):
        """A file written after construction is still seen."""
        blocked = tmp_path / "blocked.json"
        backend = MockFirewall(str(blocked))
        blocked.write_text(json.dumps([{"ip": "192.168.1.9"}]))

        assert backend.get_status()["blocked_count"] == 1

    def test_blocked_devices_stays_assignable(self, tmp_path):
        """The property needs a setter; unblock_device and a test both assign."""
        backend = MockFirewall(str(tmp_path / "blocked.json"))
        backend.blocked_devices = [{"ip": "10.0.0.5"}]
        assert backend.get_status()["blocked_count"] == 1


class TestBlockedListFollowsTheStateDir:
    def test_block_is_persisted_into_a_state_dir_that_does_not_exist_yet(
            self, tmp_path, monkeypatch):
        """The migration's whole point: a block must survive a restart.

        The state root is deliberately absent -- a fresh appliance has not
        created it yet, and a block that raises here is a block that silently
        does not persist.
        """
        state = tmp_path / "state" / "nested"
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(state))
        assert not state.exists()
        backend = MockFirewall()

        result = backend.block_device("192.168.1.77", "probe")

        assert result["success"] is True
        blocked = state / "blocked_devices.json"
        assert blocked.is_file()
        assert json.loads(blocked.read_text())[0]["ip"] == "192.168.1.77"

    def test_nothing_is_written_to_the_repo_tree(self, tmp_path, monkeypatch):
        """Guard for these tests themselves, not just for the product."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        repo_copy = os.path.join(fw_mod._REPO_ROOT, "blocked_devices.json")
        before = os.path.exists(repo_copy)

        MockFirewall().block_device("192.168.1.78", "probe")

        assert os.path.exists(repo_copy) is before


class TestPathValuedOverrides:
    """An override written as a Path is an override, not a typo to ignore.

    Set these with mock.patch, NOT monkeypatch.setattr: the names come from a
    PEP 562 module __getattr__, and monkeypatch records the resolved value with
    getattr and re-assigns it on teardown, pinning a permanent override into
    the module for the rest of the session.
    """

    def test_path_override_wins_over_the_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        pinned = tmp_path / "pinned.json"
        with patch.object(fw_mod, "_BLOCKED_FILE", pinned):
            assert fw_mod._blocked_file() == str(pinned)

    def test_path_override_reaches_the_backend(self, tmp_path):
        pinned = tmp_path / "pinned.json"
        backend = MockFirewall(str(tmp_path / "blocked.json"))
        with patch.object(fw_mod, "_RULES_FILE", pinned):
            backend.add_rule("allow_ssh", "inbound", "allow", "TCP", "22")

        assert json.loads(pinned.read_text())[0]["name"] == "allow_ssh"

    def test_a_non_path_override_is_ignored(self, monkeypatch):
        """@patch(...) with no `new` leaves a MagicMock, which answers
        __fspath__ with a junk name -- honouring it would write the block list
        to a junk path relative to the CWD."""
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        with patch.object(fw_mod, "_BLOCKED_FILE", MagicMock()):
            assert fw_mod._blocked_file() == os.path.join(
                fw_mod._REPO_ROOT, "blocked_devices.json")

    def test_empty_override_is_ignored(self, monkeypatch):
        """An empty string is not a path; it would write to the CWD."""
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        with patch.object(fw_mod, "_BLOCKED_FILE", ""):
            assert fw_mod._blocked_file() == os.path.join(
                fw_mod._REPO_ROOT, "blocked_devices.json")

    def test_path_ctor_argument_still_works(self, tmp_path):
        backend = MockFirewall(Path(tmp_path / "blocked.json"))
        assert backend._blocked_file == str(tmp_path / "blocked.json")
