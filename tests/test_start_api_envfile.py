"""The installers' closing instruction has to actually work.

``install.sh`` / ``install.ps1`` mint ``<state>/homebot.env`` (0600) holding
``PUFFPAI_API_KEY=<64 hex>`` and close with ``python start_api.py``. Nothing
loaded that file, and ``api_server`` resolves the key at IMPORT time and treats
a miss as fatal — so the documented first command died on a fresh install, with
an error naming ``/etc/puffpai/api.env`` on a box with no such path.

These tests pin the loader (:mod:`puffpai.envfile`), the precedence decision
that goes with it (the install's env file beats a pre-existing OS-keychain
entry), and the two properties that make both safe: an already-set environment
variable is never overridden, and no secret value is ever logged or returned in
a diagnostic.

No network, no real keyring (the suite's conftest disables it; the keyring tier
is stubbed explicitly where it matters), no writes outside tmp_path.
"""
from __future__ import annotations

import logging
import os

import pytest

# Not a real credential: 64 hex characters, the shape `openssl rand -hex 32`
# makes, generated for this file only.
_FAKE_KEY = "a1" * 32
_OTHER_KEY = "b2" * 32
_KEYCHAIN_KEY = "c3" * 32


@pytest.fixture(autouse=True)
def _forget_env_file_provenance():
    """The provenance registry is process-global; keep tests independent."""
    from puffpai.security import secrets_provider as sp
    sp.reset_env_file_keys()
    yield
    sp.reset_env_file_keys()


def _write_env(tmp_path, body: str):
    from puffpai.envfile import ENV_FILENAME
    path = tmp_path / ENV_FILENAME
    path.write_text(body, encoding="utf-8")
    return str(path)


class TestTheLoader:
    def test_it_sets_variables_from_the_file(self, tmp_path, monkeypatch):
        from puffpai.envfile import load_env_file
        # delenv (rather than os.environ.pop) so the session conftest's seeded
        # PUFFPAI_API_KEY is RESTORED at teardown: popping it broke every later
        # test that imports api_server.
        monkeypatch.delenv("PUFFPAI_API_KEY", raising=False)
        monkeypatch.delenv("PUFFPAI_API_BIND", raising=False)
        path = _write_env(tmp_path, f"PUFFPAI_API_KEY={_FAKE_KEY}\n"
                                    "PUFFPAI_API_BIND=127.0.0.1\n")
        names = load_env_file(path)
        assert names == ["PUFFPAI_API_BIND", "PUFFPAI_API_KEY"]
        assert os.environ["PUFFPAI_API_KEY"] == _FAKE_KEY

    def test_it_never_overrides_an_existing_variable(self, tmp_path,
                                                     monkeypatch):
        """systemd's EnvironmentFile, a container --env and an operator's
        export all outrank a file on disk."""
        from puffpai.envfile import load_env_file
        monkeypatch.setenv("PUFFPAI_API_KEY", _OTHER_KEY)
        path = _write_env(tmp_path, f"PUFFPAI_API_KEY={_FAKE_KEY}\n")
        assert load_env_file(path) == []
        assert os.environ["PUFFPAI_API_KEY"] == _OTHER_KEY

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        from puffpai.envfile import load_env_file
        assert load_env_file(str(tmp_path / "nope.env")) == []

    @pytest.mark.parametrize("body,expected", [
        ('PUFFPAI_API_KEY="' + _FAKE_KEY + '"\n', _FAKE_KEY),
        ("PUFFPAI_API_KEY='" + _FAKE_KEY + "'\n", _FAKE_KEY),
        (f"export PUFFPAI_API_KEY={_FAKE_KEY}\n", _FAKE_KEY),
        (f"# a comment\n\n  PUFFPAI_API_KEY={_FAKE_KEY}  \n", _FAKE_KEY),
    ])
    def test_the_fallback_parser_handles_the_real_shapes(self, body, expected):
        from puffpai.envfile import _parse
        assert _parse(body)["PUFFPAI_API_KEY"] == expected

    def test_the_fallback_parser_does_not_expand_variables(self):
        """A '$' in a generated key must survive verbatim."""
        from puffpai.envfile import _parse
        assert _parse("HA_TOKEN=lit$HOME\n")["HA_TOKEN"] == "lit$HOME"

    def test_the_log_line_names_variables_and_never_values(self, tmp_path,
                                                           caplog, monkeypatch):
        from puffpai.envfile import load_env_file
        monkeypatch.delenv("PUFFPAI_API_KEY", raising=False)
        path = _write_env(tmp_path, f"PUFFPAI_API_KEY={_FAKE_KEY}\n")
        with caplog.at_level(logging.DEBUG, logger="puffpai.envfile"):
            load_env_file(path)
        text = caplog.text
        assert "PUFFPAI_API_KEY" in text
        assert _FAKE_KEY not in text

    def test_the_path_follows_the_data_dir(self, tmp_path, monkeypatch):
        from puffpai.envfile import ENV_FILENAME, state_env_path
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        assert state_env_path() == os.path.join(str(tmp_path), ENV_FILENAME)

    def test_the_path_falls_back_to_the_per_user_dir(self, monkeypatch):
        """The default install location — what install.ps1/sh use with no
        $PUFFPAI_DATA_DIR."""
        from puffpai.envfile import state_env_path
        from puffpai.platform_paths import user_data_dir
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        assert state_env_path() == os.path.join(user_data_dir(), "homebot.env")


class TestStartApiCallsTheLoaderFirst:
    def test_load_install_env_is_invoked_before_api_server_import(self):
        """Order is the whole fix: api_server resolves the key at import."""
        import ast
        import pathlib
        source = pathlib.Path(__file__).resolve().parents[1] / "start_api.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        main = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        loader_line = None
        import_line = None
        for node in ast.walk(main):
            if (loader_line is None and isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_load_install_env"):
                loader_line = node.lineno
            if (import_line is None and isinstance(node, ast.ImportFrom)
                    and node.module == "puffpai.api_server"):
                import_line = node.lineno
        assert loader_line is not None, "start_api.main() no longer loads the env file"
        assert import_line is not None
        assert loader_line < import_line, (
            "the env file must be loaded BEFORE api_server is imported — "
            "api_server resolves PUFFPAI_API_KEY at import time and a miss is "
            "fatal")

    def test_the_loader_is_not_fatal(self, monkeypatch):
        import start_api
        monkeypatch.setattr("puffpai.envfile.load_state_env",
                            lambda: (_ for _ in ()).throw(OSError("boom")))
        assert start_api._load_install_env() == ("", [])


class TestTheEnvFileBeatsTheKeychain:
    """A stale OS-keychain entry from another install must not shadow the key
    the installer just minted — that returned 401 for the documented key."""

    def _provider(self, monkeypatch, keychain_value):
        from puffpai.security import secrets_provider as sp
        monkeypatch.setattr(sp, "_HAS_KEYRING", True)
        monkeypatch.setattr(
            sp, "keyring",
            type("_K", (), {"get_password": staticmethod(
                lambda service, name: keychain_value)})(),
            raising=False)
        return sp

    def test_env_file_key_wins_over_a_pre_existing_keychain_entry(
            self, tmp_path, monkeypatch):
        sp = self._provider(monkeypatch, _KEYCHAIN_KEY)
        monkeypatch.delenv("PUFFPAI_API_KEY", raising=False)
        from puffpai.envfile import load_env_file
        load_env_file(_write_env(tmp_path, f"PUFFPAI_API_KEY={_FAKE_KEY}\n"))
        value, source = sp.SecretsProvider().get_with_source(
            "PUFFPAI_API_KEY")
        assert value == _FAKE_KEY
        assert source == "env file"

    def test_a_plain_environment_variable_does_NOT_get_the_promotion(
            self, monkeypatch):
        """Only the env file the loader read is promoted. A stray export in a
        shell profile stays at the bottom tier, below the keychain, exactly as
        before — the promotion is about provenance, not about os.environ."""
        sp = self._provider(monkeypatch, _KEYCHAIN_KEY)
        monkeypatch.setenv("PUFFPAI_API_KEY", _OTHER_KEY)
        value, source = sp.SecretsProvider().get_with_source("PUFFPAI_API_KEY")
        assert value == _KEYCHAIN_KEY
        assert source == "OS keyring"

    def test_the_vault_still_outranks_the_env_file(self, tmp_path, monkeypatch):
        from puffpai.security import secrets_provider as sp
        from puffpai.envfile import load_env_file
        monkeypatch.delenv("PUFFPAI_API_KEY", raising=False)
        load_env_file(_write_env(tmp_path, f"PUFFPAI_API_KEY={_FAKE_KEY}\n"))
        vault = type("_V", (), {"get_service_secret": staticmethod(
            lambda name: _OTHER_KEY)})()
        value, source = sp.SecretsProvider(vault=vault).get_with_source(
            "PUFFPAI_API_KEY")
        assert value == _OTHER_KEY
        assert source == "vault"

    def test_a_vault_only_deployment_refuses_the_env_file_tier(
            self, tmp_path, monkeypatch):
        """allow_env_fallback=False means no plaintext tier at all — the
        promotion re-RANKS the env file, it does not re-enable it."""
        from puffpai.security import secrets_provider as sp
        from puffpai.envfile import load_env_file
        monkeypatch.delenv("PUFFPAI_API_KEY", raising=False)
        load_env_file(_write_env(tmp_path, f"PUFFPAI_API_KEY={_FAKE_KEY}\n"))
        value, source = sp.SecretsProvider(
            allow_env_fallback=False, allow_keyring=False
        ).get_with_source("PUFFPAI_API_KEY")
        assert value is None
        assert source == "default"

    def test_the_source_is_reported_and_the_value_is_not(self, tmp_path,
                                                         monkeypatch, caplog):
        from puffpai.security import secrets_provider as sp
        from puffpai.envfile import load_env_file
        monkeypatch.delenv("PUFFPAI_API_KEY", raising=False)
        load_env_file(_write_env(tmp_path, f"PUFFPAI_API_KEY={_FAKE_KEY}\n"))
        with caplog.at_level(logging.DEBUG):
            _, source = sp.SecretsProvider().get_with_source(
                "PUFFPAI_API_KEY")
        assert source == "env file"
        assert _FAKE_KEY not in caplog.text


def test_api_server_exposes_the_key_source_without_the_key(monkeypatch):
    """start_api prints this at boot so a 401 on the documented key is one
    line to diagnose."""
    from tests.modpatch import patched_modules
    from unittest.mock import MagicMock
    import importlib
    import sys
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules(
            {"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        mod = (importlib.reload(sys.modules["puffpai.api_server"])
               if "puffpai.api_server" in sys.modules
               else importlib.import_module("puffpai.api_server"))
    source = mod.api_key_source()
    assert isinstance(source, str) and source
    assert source in {"vault", "env file", "OS keyring", "environment",
                      "legacy data/api_key.txt",
                      "newly provisioned OS keyring entry", "unknown"}
    assert mod._API_KEY not in source
