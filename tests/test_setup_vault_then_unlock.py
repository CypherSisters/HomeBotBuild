"""The Vault the browser setup seals opens at once, in the same process.

Setup seals the vault through a handle of its own, while the server keeps the
handle it made at start, which read "not set up" then. Before the fix the
owner's first unlock answered "Vault not initialized" until a restart. These
tests run the owner's path in one app: the first-run code, the seal, the typed
back recovery code, then the unlock with the same PIN.

No network; every test has its own state and config folders.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

PIN = "vault-pin-setup-7"
REMOTE = {"REMOTE_ADDR": "192.168.1.66"}


@pytest.fixture
def scratch(tmp_path):
    """One test, its own install: temp state dir AND temp config dir."""
    from puffpai.config import CONFIG_DIR_ENV, get_config

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
def client(scratch):
    from puffpai import api_server
    api_server._vault_instance = None
    api_server._vault_made_for = None
    api_server._vault_sessions.clear()
    api_server._setup_recovery_pending.clear()
    api_server.app.config.update(TESTING=True)
    yield api_server.app.test_client()
    api_server._vault_sessions.clear()
    api_server._setup_recovery_pending.clear()
    api_server._vault_instance = None
    api_server._vault_made_for = None


def _auth(extra=None):
    from puffpai import api_server
    headers = {"Authorization": f"Bearer {api_server._API_KEY}"}
    headers.update(extra or {})
    return headers


def _first_run_code():
    from puffpai.security import setup_code
    value = setup_code.current()
    assert value, "setup should be incomplete in a fresh scratch install"
    return value


def _startup_handle():
    """The handle the server makes at start, before any vault exists."""
    from puffpai import api_server
    vault = api_server._get_vault()
    assert vault is not None
    assert vault.is_initialized is False
    return vault


def test_setup_then_confirm_then_unlock_works_without_a_restart(client):
    _startup_handle()
    code = _first_run_code()
    sealed = client.post("/setup/vault", json={"pin": PIN},
                         headers=_auth({"X-Setup-Code": code}),
                         environ_base=REMOTE)
    assert sealed.status_code == 200, sealed.get_json()
    recovery = sealed.get_json()["recovery_code"]

    confirmed = client.post("/setup/vault/confirm",
                            json={"recovery_code": recovery},
                            headers=_auth({"X-Setup-Code": code}),
                            environ_base=REMOTE)
    assert confirmed.status_code == 200, confirmed.get_json()

    opened = client.post("/vault/unlock", json={"pin": PIN}, headers=_auth())
    body = opened.get_json()
    assert opened.status_code == 200, body
    assert body["success"] is True
    assert body["session_token"]
    assert not body.get("needs_initialization")


def test_a_wrong_pin_after_setup_is_still_refused(client):
    _startup_handle()
    resp = client.post("/setup/vault", json={"pin": PIN},
                       headers=_auth({"X-Setup-Code": _first_run_code()}),
                       environ_base=REMOTE)
    assert resp.status_code == 200, resp.get_json()
    wrong = client.post("/vault/unlock", json={"pin": PIN + "x"},
                        headers=_auth())
    assert wrong.status_code == 401
    assert not wrong.get_json().get("session_token")


def test_a_vault_sealed_by_another_handle_is_seen_on_the_next_unlock(client):
    """The console setup seals the vault in another process; the running
    server must pick it up without a restart as well."""
    from puffpai import setup_ops

    _startup_handle()
    setup_ops.seal_vault_with_recovery(PIN)
    opened = client.post("/vault/unlock", json={"pin": PIN}, headers=_auth())
    assert opened.status_code == 200, opened.get_json()
    assert opened.get_json()["success"] is True


def test_an_unreadable_key_file_is_not_reloaded_on_every_request(client,
                                                                  monkeypatch):
    from puffpai import api_server

    vault = _startup_handle()
    Path(vault._key_file).write_bytes(b"short")
    calls = []
    real = api_server._reload_vault_handle
    monkeypatch.setattr(api_server, "_reload_vault_handle",
                        lambda reason="moved": calls.append(reason) or real(reason))
    for _ in range(3):
        api_server._get_vault()
    assert calls == ["setup"]


def test_the_not_set_up_reply_is_plain_words(client):
    _startup_handle()
    resp = client.post("/vault/unlock", json={"pin": PIN}, headers=_auth())
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["needs_initialization"] is True
    message = body["message"]
    for raw in ("{", "}", "initialize", "Send", "not initialized"):
        assert raw not in message, message
