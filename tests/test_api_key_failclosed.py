"""Fail-closed contract for api_server._load_or_create_api_key().

The historical bug: on a total resolution miss the function minted a new key
and wrote it into the OS keyring, overwriting the machine's REAL shared
PUFFPAI_API_KEY entry (Windows Credential Manager service "puffpai"), then
fell back to recreating the retired plaintext data/api_key.txt. "Entry exists
but resolution failed" was indistinguishable from "first boot", so a transient
failure destroyed the production credential and broke every paired client.

These tests pin the new contract: resolve read-only, refuse to mint unless
PUFFPAI_PROVISION_API_KEY=1 AND the keyring positively confirms no entry, and
never write data/api_key.txt under any circumstance.

Isolation: the function's provisioning branch does a LOCAL ``import keyring``,
so every test that can reach it stubs ``sys.modules["keyring"]``. No test here
may reach the real Credential Manager — the conftest raiser is a backstop, not
the primary isolation. _API_KEY_PATH is repointed into tmp_path so no test can
recreate the repo's retired plaintext file (asserted after every test).
"""
import importlib
import os
import re
import sys
import warnings
from unittest.mock import MagicMock

import pytest

from tests._secret_asserts import _assert_secret_is
from tests.modpatch import patched_modules

# Test literals. None of these is a real credential.
_SYNTHETIC_EXISTING = "synthetic-preexisting-keyring-value-not-a-real-secret"
_SYNTHETIC_ENV = "synthetic-env-tier-value-not-a-real-secret"
_SYNTHETIC_FILE = "synthetic-legacy-file-value-not-a-real-secret"


@pytest.fixture(scope="module")
def mod():
    """Import api_server once, with firewall_manager mocked out.

    Same recipe as tests/test_secret_hygiene.py. The import-time
    ``_API_KEY = _load_or_create_api_key()`` resolves via the env tier that the
    session-scoped conftest guard pre-seeds, so importing never reaches the
    provisioning paths under test.
    """
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules(
            {"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        if "puffpai.api_server" in sys.modules:
            return importlib.reload(sys.modules["puffpai.api_server"])
        return importlib.import_module("puffpai.api_server")


@pytest.fixture(autouse=True)
def _repo_plaintext_file_stays_gone(mod):
    """No test in this file may (re)create the repo's retired api_key.txt."""
    real_path = os.path.join(
        mod._REPO_ROOT_EARLY, "data", "api_key.txt")
    existed_before = os.path.exists(real_path)
    yield
    assert os.path.exists(real_path) == existed_before, (
        f"a test changed the existence of the retired plaintext file "
        f"{real_path} — it must never be written")


@pytest.fixture
def key_path(mod, tmp_path, monkeypatch):
    """Repoint the legacy-file tier at a nonexistent tmp path.

    The parent directory does not exist either, which also pins that removing
    the old ``os.makedirs`` call did not break the read path.
    """
    path = tmp_path / "data" / "api_key.txt"
    monkeypatch.setattr(mod, "_API_KEY_PATH", str(path))
    return path


@pytest.fixture
def chain_miss(key_path, monkeypatch):
    """Force every resolution tier to miss.

    vault tier: no vault installed on the default provider (None).
    keyring tier: off suite-wide via conftest's ``_HAS_KEYRING = False`` —
        asserted here, because if that guard regressed this fixture would aim
        the chain at the real Credential Manager.
    env tier: PUFFPAI_API_KEY removed.
    legacy file: key_path does not exist.
    """
    import puffpai.security.secrets_provider as sp_mod
    assert sp_mod._HAS_KEYRING is False, (
        "conftest secret-hygiene guard regressed; refusing to run tests that "
        "would consult the real OS keychain")
    monkeypatch.delenv("PUFFPAI_API_KEY", raising=False)
    monkeypatch.delenv("PUFFPAI_PROVISION_API_KEY", raising=False)
    return key_path


def _keyring_stub(*, get_password=None, get_password_raises=None):
    """A stand-in for the ``keyring`` module the function imports locally."""
    stub = MagicMock(name="keyring_stub")
    if get_password_raises is not None:
        stub.get_password.side_effect = get_password_raises
    else:
        stub.get_password.return_value = get_password
    return stub


class TestFailsClosedOnMiss:
    def test_total_miss_without_flag_raises_actionable_error(
            self, mod, chain_miss, monkeypatch):
        stub = _keyring_stub()
        monkeypatch.setitem(sys.modules, "keyring", stub)

        with pytest.raises(RuntimeError) as excinfo:
            mod._load_or_create_api_key()

        message = str(excinfo.value)
        assert "refusing" in message.lower()
        # Names the env file THIS install would load (puffpai.envfile), not the
        # appliance's /etc/puffpai/api.env, which a laptop install does not have.
        from puffpai.envfile import ENV_FILENAME
        assert ENV_FILENAME in message
        assert "PUFFPAI_PROVISION_API_KEY" in message
        assert not chain_miss.exists(), "the legacy plaintext file was written"
        stub.set_password.assert_not_called()

    def test_get_secret_raising_still_fails_closed(
            self, mod, chain_miss, monkeypatch):
        import puffpai.security.secrets_provider as sp_mod

        def _boom(*a, **k):
            raise RuntimeError("secrets_provider exploded")

        # The resolver calls get_secret_with_source (it reports WHICH tier
        # answered at startup); get_secret is patched too so this test keeps
        # failing closed whichever entry point the loader uses.
        monkeypatch.setattr(sp_mod, "get_secret_with_source", _boom)
        monkeypatch.setattr(sp_mod, "get_secret", _boom)
        stub = _keyring_stub()
        monkeypatch.setitem(sys.modules, "keyring", stub)

        with pytest.raises(RuntimeError) as excinfo:
            mod._load_or_create_api_key()

        assert "refusing" in str(excinfo.value).lower()
        assert not chain_miss.exists()
        assert not chain_miss.parent.exists(), (
            "the data/ directory was recreated; the makedirs write-path "
            "scaffolding should be gone")
        stub.set_password.assert_not_called()


class TestProvisioningOptIn:
    def test_existing_keyring_entry_blocks_provisioning(
            self, mod, chain_miss, monkeypatch):
        monkeypatch.setenv("PUFFPAI_PROVISION_API_KEY", "1")
        stub = _keyring_stub(get_password=_SYNTHETIC_EXISTING)
        monkeypatch.setitem(sys.modules, "keyring", stub)

        with pytest.raises(RuntimeError) as excinfo:
            mod._load_or_create_api_key()

        message = str(excinfo.value)
        assert "EXISTS" in message
        assert "refusing" in message.lower()
        # The refusal must never echo the credential it declined to overwrite.
        assert _SYNTHETIC_EXISTING not in message
        stub.set_password.assert_not_called()
        assert not chain_miss.exists()

    def test_unreadable_keyring_blocks_provisioning(
            self, mod, chain_miss, monkeypatch):
        monkeypatch.setenv("PUFFPAI_PROVISION_API_KEY", "1")
        stub = _keyring_stub(get_password_raises=OSError("backend broken"))
        monkeypatch.setitem(sys.modules, "keyring", stub)

        with pytest.raises(RuntimeError) as excinfo:
            mod._load_or_create_api_key()

        assert "cannot confirm no entry exists" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, OSError)
        stub.set_password.assert_not_called()
        assert not chain_miss.exists()

    def test_missing_keyring_module_blocks_provisioning(
            self, mod, chain_miss, monkeypatch):
        monkeypatch.setenv("PUFFPAI_PROVISION_API_KEY", "1")
        # A None entry in sys.modules makes ``import keyring`` raise ImportError.
        monkeypatch.setitem(sys.modules, "keyring", None)

        with pytest.raises(RuntimeError) as excinfo:
            mod._load_or_create_api_key()

        message = str(excinfo.value)
        assert "keyring" in message
        # No plaintext fallback in any branch.
        assert not chain_miss.exists()

    def test_empty_keyring_provisions_once(self, mod, chain_miss, monkeypatch):
        monkeypatch.setenv("PUFFPAI_PROVISION_API_KEY", "1")
        stub = _keyring_stub(get_password=None)
        monkeypatch.setitem(sys.modules, "keyring", stub)

        minted = mod._load_or_create_api_key()

        assert re.fullmatch(r"[0-9a-f]{64}", minted) is not None
        assert stub.set_password.call_count == 1
        service, entry, stored = stub.set_password.call_args.args
        assert (service, entry) == ("puffpai", "PUFFPAI_API_KEY")
        stored_matches = stored == minted
        del stored, minted  # a freshly minted key leaves no test-frame local
        assert stored_matches, (
            "keyring.set_password did not receive the returned key")
        assert not chain_miss.exists(), "provisioning must not write a file"


class TestReadTiersStillWork:
    def test_env_tier_resolves(self, mod, key_path, monkeypatch):
        """Orin parity: PUFFPAI_API_KEY from /etc/puffpai/api.env still works."""
        monkeypatch.delenv("PUFFPAI_PROVISION_API_KEY", raising=False)
        monkeypatch.setenv("PUFFPAI_API_KEY", _SYNTHETIC_ENV)
        monkeypatch.setitem(sys.modules, "keyring", _keyring_stub())

        with warnings.catch_warnings():
            # The env tier is the deprecated plaintext path and says so.
            warnings.simplefilter("ignore", DeprecationWarning)
            _assert_secret_is(mod._load_or_create_api_key(), _SYNTHETIC_ENV,
                              "api key resolved via the env tier")

    def test_legacy_file_tier_is_read_only(
            self, mod, chain_miss, monkeypatch):
        chain_miss.parent.mkdir(parents=True, exist_ok=True)
        chain_miss.write_text(_SYNTHETIC_FILE + "\n", encoding="utf-8")
        monkeypatch.setitem(sys.modules, "keyring", _keyring_stub())

        _assert_secret_is(mod._load_or_create_api_key(), _SYNTHETIC_FILE,
                          "api key resolved via the legacy file tier")
        assert chain_miss.read_text(encoding="utf-8") == _SYNTHETIC_FILE + "\n"
