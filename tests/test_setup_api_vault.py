"""Zone 1 through the browser face: sealing the vault and typing the code back.

The recovery code is the one value in this whole surface that CANNOT be
re-issued from anything the box keeps. So:

* it is returned exactly once, on the response to the call that minted it;
* what the server keeps afterwards is a DIGEST, not the code — the typed-back
  check runs server-side without the server holding the secret;
* wrong answers are throttled by the VAULT's own numbers, and exhausting them
  destroys the slot rather than letting a guesser keep going;
* re-sealing through /setup is impossible once setup is finished, with no
  vault-session bypass — that route would destroy a household's data under a
  PIN the caller chose.

Run:
    pytest tests/test_setup_api_vault.py
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from puffpai import setup_ops
from puffpai.config import get_config

PIN = "vault-pin-setup-1"


@pytest.fixture
def scratch(tmp_path):
    """One test, its own install: temp state dir AND temp config dir.

    Both are load-bearing. ``$PUFFPAI_DATA_DIR`` alone is NOT enough: the
    wizard writes through ``setup_ops.write_main_config`` /
    ``set_ha_config``, which resolve to ``cfg.CONFIG_DIR`` — the REPO's
    ``config/`` unless ``$PUFFPAI_CONFIG_DIR`` says otherwise. Without the
    second variable these tests rewrite the shipped
    ``config/puffpai_config.json`` and drop a ``home_assistant_config.json``
    into the checkout (observed: identity, storage and HA answers landing in
    git status).

    The shipped config is COPIED in, so the wizard merges into the same base a
    real install has rather than starting from an empty file.
    """
    from puffpai.config import CONFIG_DIR_ENV

    repo_config = Path(__file__).resolve().parents[1] / "config"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for name in ("puffpai_config.json", "bridge_postures.json",
                 "capability_catalog.json", "model_catalog.json",
                 "model_profiles.json"):
        source = repo_config / name
        if source.exists():
            shutil.copy2(source, config_dir / name)

    previous = {key: os.environ.get(key)
                for key in ("PUFFPAI_DATA_DIR", CONFIG_DIR_ENV)}
    os.environ["PUFFPAI_DATA_DIR"] = str(tmp_path)
    os.environ[CONFIG_DIR_ENV] = str(config_dir)
    get_config(_force_reload=True)
    try:
        yield tmp_path
    finally:
        for key, value in previous.items():
            if value is not None:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        get_config(_force_reload=True)


@pytest.fixture
def client():
    """A test client with the process-global vault handle dropped.

    ``api_server._get_vault()`` caches ONE Vault bound to whatever directory
    was resolved the first time it was called. Under `scratch` each test has
    its own vault dir, so without this reset a vault sealed in this test is
    unlocked against the previous test's directory - an order dependence that
    passes alone and fails in a file.
    """
    from puffpai import api_server
    api_server._vault_instance = None
    api_server._vault_sessions.clear()
    api_server.app.config.update(TESTING=True)
    api_server._setup_recovery_pending.clear()
    yield api_server.app.test_client()
    api_server._setup_recovery_pending.clear()


def _auth():
    from puffpai import api_server
    return {"Authorization": f"Bearer {api_server._API_KEY}"}


def _seal(client, pin=PIN):
    return client.post("/setup/vault", json={"pin": pin}, headers=_auth())


class TestSealing:

    def test_seal_returns_the_code_once_and_no_store(self, client, scratch):
        resp = _seal(client)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["ok"] is True
        assert body["recovery_issued"] is True
        assert body["recovery_code"]
        assert isinstance(body["recovery_code_notice"], list)
        assert "no-store" in resp.headers.get("Cache-Control", "")
        assert setup_ops.vault_initialized() is True

    def test_the_code_is_never_returned_again(self, client, scratch):
        first = _seal(client).get_json()["recovery_code"]
        again = _seal(client)
        assert again.status_code == 409
        text = again.get_data(as_text=True)
        assert again.get_json()["error"] == "vault_exists"
        assert first not in text

    def test_a_weak_pin_is_refused_without_quoting_it(self, client, scratch):
        resp = client.post("/setup/vault", json={"pin": "1234"}, headers=_auth())
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["error"] == "weak_pin"
        assert "1234" not in body["message"]
        assert setup_ops.vault_initialized() is False

    def test_a_missing_pin_is_refused(self, client, scratch):
        resp = client.post("/setup/vault", json={}, headers=_auth())
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "pin_required"

    def test_buildbots_password_spelling_still_works(self, client, scratch):
        resp = client.post("/setup/vault", json={"password": PIN},
                           headers=_auth())
        assert resp.status_code == 200
        assert resp.get_json()["recovery_code"]

    def test_the_manifest_block_carries_no_secret(self, client, scratch):
        body = _seal(client).get_json()
        block = body["vault"]
        flat = repr(block)
        assert PIN not in flat
        assert body["recovery_code"] not in flat


class TestTypedBackConfirmation:

    def test_the_right_code_confirms(self, client, scratch):
        code = _seal(client).get_json()["recovery_code"]
        resp = client.post("/setup/vault/confirm", json={"recovery_code": code},
                           headers=_auth())
        assert resp.status_code == 200
        assert resp.get_json()["confirmed"] is True
        assert "no-store" in resp.headers.get("Cache-Control", "")

    def test_handwriting_tolerance_matches_the_wizards_rule(self, client, scratch):
        """Dashes and case are normalised away - exactly as the CLI face does."""
        from puffpai import api_server

        code = _seal(client).get_json()["recovery_code"]
        variants = [code.lower(), code.replace("-", ""), f"  {code}  ",
                    "-".join(code.replace("-", "")[i:i + 4]
                             for i in range(0, len(code.replace("-", "")), 4))]
        for variant in variants:
            # The route's digest check and the wizard's own comparison must
            # agree, or the restatement in api_server has drifted.
            assert setup_ops.confirm_vault_recovery(code, variant) is True
            assert (api_server._setup_recovery_digest(variant)
                    == api_server._setup_recovery_digest(code)), variant

    def test_a_wrong_code_is_refused_and_says_nothing_about_the_right_one(
            self, client, scratch):
        code = _seal(client).get_json()["recovery_code"]
        resp = client.post("/setup/vault/confirm",
                           json={"recovery_code": "WRONG-CODE-ENTIRELY"},
                           headers=_auth())
        assert resp.status_code == 400
        text = resp.get_data(as_text=True)
        assert resp.get_json()["error"] == "recovery_code_mismatch"
        assert code not in text
        assert code.replace("-", "") not in text

    def test_attempts_are_throttled_by_the_vaults_own_numbers(self, client,
                                                              scratch):
        from puffpai.security import vault as vault_mod

        _seal(client)
        last = None
        for _ in range(vault_mod._MAX_FAILED_ATTEMPTS):
            last = client.post("/setup/vault/confirm",
                               json={"recovery_code": "NOPE"}, headers=_auth())
        assert last.status_code == 429
        assert last.get_json()["error"] == "too_many_attempts"

    def test_exhausting_the_attempts_destroys_the_slot(self, client, scratch):
        """Fail closed: the way on is a fresh code, not more guesses."""
        from puffpai.security import vault as vault_mod

        code = _seal(client).get_json()["recovery_code"]
        for _ in range(vault_mod._MAX_FAILED_ATTEMPTS):
            client.post("/setup/vault/confirm", json={"recovery_code": "NOPE"},
                        headers=_auth())
        resp = client.post("/setup/vault/confirm", json={"recovery_code": code},
                           headers=_auth())
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "no_pending_confirmation"

    def test_confirming_with_nothing_pending_is_refused(self, client, scratch):
        resp = client.post("/setup/vault/confirm", json={"recovery_code": "X"},
                           headers=_auth())
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "no_pending_confirmation"

    def test_an_expired_slot_is_refused(self, client, scratch, monkeypatch):
        from puffpai import api_server

        code = _seal(client).get_json()["recovery_code"]
        api_server._setup_recovery_pending["issued_at"] = (
            api_server._setup_recovery_pending["issued_at"]
            - api_server._SETUP_RECOVERY_TTL - 1)
        resp = client.post("/setup/vault/confirm", json={"recovery_code": code},
                           headers=_auth())
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "confirmation_expired"

    def test_the_server_keeps_a_digest_not_the_code(self, client, scratch):
        from puffpai import api_server

        code = _seal(client).get_json()["recovery_code"]
        slot = api_server._setup_recovery_pending
        assert isinstance(slot["digest"], bytes)
        blob = repr(slot)
        assert code not in blob
        assert code.replace("-", "") not in blob


class TestResealIsImpossibleAfterCompletion:

    def test_seal_is_refused_once_setup_is_complete(self, client, scratch):
        setup_ops.mark_complete()
        resp = client.post("/setup/vault", json={"pin": PIN}, headers=_auth())
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "setup_complete"
        assert setup_ops.vault_initialized() is False

    def test_a_live_vault_session_does_NOT_reopen_the_seal_route(
            self, client, scratch):
        """The non-vault routes reopen under a session. This one never does."""
        from puffpai import api_server

        setup_ops.seal_vault_with_recovery("vault-pin-owner-2")
        token = client.post("/vault/unlock", json={"pin": "vault-pin-owner-2"},
                            headers=_auth()).get_json()["session_token"]
        setup_ops.mark_complete()
        headers = dict(_auth())
        headers["X-Vault-Token"] = token
        for path in ("/setup/vault", "/setup/vault/confirm"):
            resp = client.post(path, json={"pin": PIN, "recovery_code": "X"},
                               headers=headers)
            assert resp.status_code == 409, path
            assert resp.get_json()["error"] == "setup_complete"
        api_server._vault_sessions.pop(token, None)


class TestVaultPinFloor:
    """The vault's own floor, and the invariant that the wizard is not laxer."""

    def test_the_wizard_floor_is_at_or_above_the_vault_floor(self):
        from puffpai import zone_vault
        from puffpai.security import vault as vault_mod

        assert zone_vault._MIN_PIN_LENGTH >= vault_mod.MIN_PIN_LENGTH
        assert (zone_vault._MIN_NUMERIC_PIN_LENGTH
                >= vault_mod.MIN_NUMERIC_PIN_LENGTH)

    def test_a_four_character_pin_can_no_longer_create_a_vault(self, tmp_path):
        from puffpai.security.vault import Vault

        v = Vault(vault_dir=str(tmp_path / "vault"))
        with pytest.raises(ValueError):
            v.initialize("1234")
        assert v.is_initialized is False

    def test_a_short_numeric_pin_is_refused_even_at_eight_digits(self, tmp_path):
        from puffpai.security.vault import Vault

        v = Vault(vault_dir=str(tmp_path / "vault"))
        with pytest.raises(ValueError):
            v.initialize("12345678")
        v.initialize("1234567890")
        assert v.is_initialized is True

    def test_an_existing_short_pin_vault_still_unlocks(self, tmp_path,
                                                      monkeypatch):
        """The floor applies when a PIN is SET, never when one is VERIFIED.

        Locking a household out of its own data to enforce a policy change
        would be the worse failure, so this is the direction that matters.
        """
        from puffpai.security import vault as vault_mod

        v = vault_mod.Vault(vault_dir=str(tmp_path / "vault"))
        # Create under the OLD floor, exactly as a pre-upgrade box did.
        monkeypatch.setattr(vault_mod, "_check_new_pin", lambda _pin: None)
        v.initialize("1234")
        monkeypatch.undo()

        fresh = vault_mod.Vault(vault_dir=str(tmp_path / "vault"))
        session = fresh.authenticate("1234")
        assert session is not None
        # ...but changing to another short PIN is refused.
        with pytest.raises(ValueError):
            fresh.change_pin(session.session_id, "1234", "5678")

    def test_the_refusal_never_quotes_the_pin(self, tmp_path):
        from puffpai.security.vault import Vault

        v = Vault(vault_dir=str(tmp_path / "vault"))
        with pytest.raises(ValueError) as excinfo:
            v.initialize("hunter2")
        assert "hunter2" not in str(excinfo.value)
