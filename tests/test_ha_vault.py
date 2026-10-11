"""Tests for security/ha_vault.py — HA token vault migration."""

import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path


class TestGetHaToken:
    def test_plaintext_fallback_with_real_token(self, tmp_path):
        """When vault unavailable, reads from plaintext config with warning."""
        config = {"url": "http://ha.local:8123", "token": "real_jwt_token", "enabled": True}
        config_file = tmp_path / "home_assistant_config.json"
        config_file.write_text(json.dumps(config))

        with patch("puffpai.security.ha_vault._HA_CONFIG_FILE", config_file):
            from puffpai.security.ha_vault import get_ha_token
            with pytest.warns(UserWarning, match="plaintext"):
                token = get_ha_token()
            assert token == "real_jwt_token"

    def test_placeholder_token_returns_none(self, tmp_path):
        """Placeholder default token should return None."""
        config = {"url": "http://ha.local:8123",
                  "token": "YOUR_LONG_LIVED_ACCESS_TOKEN_HERE", "enabled": False}
        config_file = tmp_path / "home_assistant_config.json"
        config_file.write_text(json.dumps(config))

        with patch("puffpai.security.ha_vault._HA_CONFIG_FILE", config_file):
            from puffpai.security.ha_vault import get_ha_token
            token = get_ha_token()
            assert token is None

    def test_vault_read_preferred(self, tmp_path):
        """When vault has a token and session is valid, prefer vault."""
        config_file = tmp_path / "home_assistant_config.json"
        config_file.write_text(json.dumps({"url": "http://ha:8123", "token": "old", "enabled": True}))

        mock_vault = MagicMock()
        mock_vault.is_initialized = True
        mock_vault.read_secure_session.return_value = {"token": "vault_jwt_token"}

        with patch("puffpai.security.ha_vault._HA_CONFIG_FILE", config_file), \
             patch("puffpai.security.ha_vault._get_vault", return_value=mock_vault):
            from puffpai.security.ha_vault import get_ha_token
            token = get_ha_token(session_id="test-session")
            assert token == "vault_jwt_token"
            mock_vault.read_secure_session.assert_called_once_with(
                "ha_token.json", "test-session")

    def test_vault_failure_falls_back_to_plaintext(self, tmp_path):
        """Locked vault (read raises, session not unlocked) falls back to plaintext."""
        from puffpai.security.vault import VaultError
        config = {"url": "http://ha:8123", "token": "fallback_token", "enabled": True}
        config_file = tmp_path / "home_assistant_config.json"
        config_file.write_text(json.dumps(config))

        mock_vault = MagicMock()
        mock_vault.is_initialized = True
        mock_vault.read_secure_session.side_effect = VaultError("locked")
        mock_vault.is_unlocked.return_value = False

        with patch("puffpai.security.ha_vault._HA_CONFIG_FILE", config_file), \
             patch("puffpai.security.ha_vault._get_vault", return_value=mock_vault):
            from puffpai.security.ha_vault import get_ha_token
            with pytest.warns(UserWarning, match="plaintext"):
                token = get_ha_token(session_id="test-session")
            assert token == "fallback_token"


class TestStoreHaToken:
    def test_store_writes_to_vault_and_scrubs_config(self, tmp_path):
        config = {"url": "http://ha:8123", "token": "old_token", "enabled": True}
        config_file = tmp_path / "home_assistant_config.json"
        config_file.write_text(json.dumps(config))

        mock_vault = MagicMock()
        mock_vault.is_initialized = True

        with patch("puffpai.security.ha_vault._HA_CONFIG_FILE", config_file), \
             patch("puffpai.security.ha_vault._get_vault", return_value=mock_vault):
            from puffpai.security.ha_vault import store_ha_token
            result = store_ha_token("new_secure_token", "test-session")
            assert result is True
            mock_vault.write_secure_session.assert_called_once_with(
                "ha_token.json", {"token": "new_secure_token"}, "test-session"
            )
            # Config file should have scrubbed token
            updated = json.loads(config_file.read_text())
            assert updated["token"] == "MIGRATED_TO_VAULT"
            assert updated["url"] == "http://ha:8123"

    def test_store_fails_when_vault_not_initialized(self):
        mock_vault = MagicMock()
        mock_vault.is_initialized = False

        with patch("puffpai.security.ha_vault._get_vault", return_value=mock_vault):
            from puffpai.security.ha_vault import store_ha_token
            with pytest.warns(UserWarning, match="not initialized"):
                result = store_ha_token("token", "session")
            assert result is False


class TestGetHaConfig:
    def test_returns_full_config_dict(self, tmp_path):
        config = {"url": "http://ha:8123", "token": "jwt_here", "enabled": True}
        config_file = tmp_path / "home_assistant_config.json"
        config_file.write_text(json.dumps(config))

        with patch("puffpai.security.ha_vault._HA_CONFIG_FILE", config_file):
            from puffpai.security.ha_vault import get_ha_config
            with pytest.warns(UserWarning, match="plaintext"):
                cfg = get_ha_config()
            assert cfg["url"] == "http://ha:8123"
            assert cfg["token"] == "jwt_here"
            assert cfg["enabled"] is True


# ── Real-Vault (no-mock) tests: exercise the encrypted round-trip end-to-end ──


def _new_live_vault(tmp_path, pin="vault-pin-1234"):
    """Initialize + authenticate a real Vault; return (vault, session_id)."""
    from puffpai.security.vault import Vault
    v = Vault(vault_dir=str(tmp_path / "vault_data"))
    v.initialize(pin)
    sess = v.authenticate(pin)
    return v, sess.session_id


def _locked_vault(tmp_path, pin="vault-pin-1234"):
    """Initialize but do NOT authenticate — vault is locked (no session key)."""
    from puffpai.security.vault import Vault
    v = Vault(vault_dir=str(tmp_path / "vault_data"))
    v.initialize(pin)
    return v


class TestRealVaultEncryptedStorage:
    def test_store_encrypts_and_roundtrips(self, tmp_path):
        """store_ha_token with a live session encrypts on disk; get round-trips."""
        v, sid = _new_live_vault(tmp_path)
        vault_file = tmp_path / "vault_data" / "ha_token.json"
        config_file = tmp_path / "home_assistant_config.json"
        config_file.write_text(json.dumps(
            {"url": "http://ha:8123", "token": "old", "enabled": True}))

        token = "eyJhbGciOi.super-secret-jwt.signature"
        with patch("puffpai.security.ha_vault._HA_CONFIG_FILE", config_file), \
             patch("puffpai.security.ha_vault._get_vault", return_value=v):
            from puffpai.security.ha_vault import store_ha_token, get_ha_token
            assert store_ha_token(token, sid) is True

            # File exists and its bytes are ciphertext (no token material).
            assert vault_file.exists()
            assert token.encode() not in vault_file.read_bytes()

            # Config scrubbed.
            assert json.loads(config_file.read_text())["token"] == "MIGRATED_TO_VAULT"

            # Round-trips back through the encrypted read path.
            assert get_ha_token(sid) == token

    def test_store_locked_returns_false_no_plaintext_write(self, tmp_path):
        """Locked vault: store returns False, warns, writes NO file at all."""
        v = _locked_vault(tmp_path)
        vault_dir = tmp_path / "vault_data"
        config_file = tmp_path / "home_assistant_config.json"
        config_file.write_text(json.dumps(
            {"url": "http://ha:8123", "token": "old", "enabled": True}))

        with patch("puffpai.security.ha_vault._HA_CONFIG_FILE", config_file), \
             patch("puffpai.security.ha_vault._get_vault", return_value=v):
            from puffpai.security.ha_vault import store_ha_token
            with pytest.warns(UserWarning):
                result = store_ha_token("secret-token", "no-such-session")
            assert result is False

        # No ha_token.json created — no plaintext write path.
        assert not (vault_dir / "ha_token.json").exists()
        names = [f.name for f in vault_dir.iterdir()
                 if not f.name.startswith(".")]
        assert names == [], f"unexpected vault files: {names}"

    def test_get_locked_falls_back_to_plaintext_config(self, tmp_path):
        """Locked vault + valid plaintext config: falls back, warns, no raise."""
        v = _locked_vault(tmp_path)
        config_file = tmp_path / "home_assistant_config.json"
        config_file.write_text(json.dumps(
            {"url": "http://ha:8123", "token": "cfg_fallback_token",
             "enabled": True}))

        with patch("puffpai.security.ha_vault._HA_CONFIG_FILE", config_file), \
             patch("puffpai.security.ha_vault._get_vault", return_value=v):
            from puffpai.security.ha_vault import get_ha_token
            with pytest.warns(UserWarning, match="plaintext"):
                token = get_ha_token("no-such-session")
            assert token == "cfg_fallback_token"

    def test_get_opportunistically_migrates_plaintext(self, tmp_path):
        """Pre-seeded plaintext ha_token.json + live session: get returns the
        token, and the file is re-encrypted in place (ciphertext, decryptable)."""
        v, sid = _new_live_vault(tmp_path)
        vault_file = tmp_path / "vault_data" / "ha_token.json"
        token = "legacy-plaintext-jwt-value"
        vault_file.write_text(json.dumps({"token": token}))
        # Sanity: starts as plaintext.
        assert token.encode() in vault_file.read_bytes()

        config_file = tmp_path / "home_assistant_config.json"
        config_file.write_text(json.dumps(
            {"url": "http://ha:8123", "token": "unused", "enabled": True}))

        with patch("puffpai.security.ha_vault._HA_CONFIG_FILE", config_file), \
             patch("puffpai.security.ha_vault._get_vault", return_value=v):
            from puffpai.security.ha_vault import get_ha_token
            assert get_ha_token(sid) == token

        # File is now ciphertext and decrypts via read_secure_session.
        assert token.encode() not in vault_file.read_bytes()
        assert v.read_secure_session("ha_token.json", sid) == {"token": token}
