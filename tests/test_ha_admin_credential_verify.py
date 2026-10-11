"""The elevated Home Assistant credential: does the vault give back what the
owner typed, and is what it holds worth anything?

Two 401s from Home Assistant on an approved change, after two separate stores,
left exactly two candidate explanations: the owner's token/account is wrong, or
the store->read path mangles the value. The first test in this file settles the
second one with the REAL vault classes on a temp vault -- no doubles, no stubs
-- because the earlier smoke ran with the vault LOCKED and so never exercised the
real write/read of this record at all.

Run: py -m pytest tests/test_ha_admin_credential_verify.py -q
"""

from __future__ import annotations

import importlib
import logging
import os
import sys

import pytest

from tests.modpatch import patched_modules
from unittest.mock import MagicMock

_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

#: Shaped like a real long-lived HA token: a JWT, base64url, two dots, no
#: whitespace, a couple of hundred characters.
REAL_SHAPED_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    + "aB3-_" * 36
    + ".s1gnatur3_Zq7X"
)


# -- 1. the round trip, on the real vault ----------------------------

@pytest.fixture
def real_vault(tmp_path, monkeypatch):
    """A REAL initialized vault with a live PIN session, wired in as the one
    ``ha_token_source`` will find."""
    from puffpai.security.vault import Vault
    from puffpai.security import ha_vault

    vault = Vault(vault_dir=tmp_path / "vault")
    vault.initialize("8241964073")
    session = vault.authenticate("8241964073")
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: vault)
    return vault, session.session_id


def test_store_then_resolve_returns_the_exact_bytes(real_vault):
    """THE round trip. Byte equality, no normalisation, no JSON wrapper."""
    vault, sid = real_vault
    from puffpai.security import ha_token_source as hts

    assert hts.store_ha_admin_token(REAL_SHAPED_TOKEN, sid) is True
    resolution = hts.resolve_ha_admin_token(sid)

    assert resolution.source == hts.SOURCE_VAULT_SESSION
    assert resolution.token == REAL_SHAPED_TOKEN
    assert len(resolution.token) == len(REAL_SHAPED_TOKEN)
    # Character-for-character, not merely "equal after a strip()".
    assert list(resolution.token) == list(REAL_SHAPED_TOKEN)


def test_round_trip_without_naming_the_session(real_vault):
    """The apply path resolves with ``session_id=None`` (it takes any live
    session). That must reach the same bytes, or an owner-stored credential is
    invisible to the very code that needs it."""
    vault, sid = real_vault
    from puffpai.security import ha_token_source as hts

    hts.store_ha_admin_token(REAL_SHAPED_TOKEN, sid)
    assert hts.resolve_ha_admin_token().token == REAL_SHAPED_TOKEN


def test_stored_record_is_ciphertext_on_disk(real_vault):
    """Belt and braces: the file must not contain the credential in clear."""
    vault, sid = real_vault
    from puffpai.security import ha_token_source as hts

    hts.store_ha_admin_token(REAL_SHAPED_TOKEN, sid)
    raw = (vault._vault_dir / hts.ADMIN_VAULT_FILENAME).read_bytes()
    assert REAL_SHAPED_TOKEN.encode() not in raw


def test_surrounding_whitespace_is_dropped_but_nothing_else(real_vault):
    """A pasted token with a trailing newline stores as the credential itself
    -- the one and only normalisation, and it cannot corrupt the value."""
    vault, sid = real_vault
    from puffpai.security import ha_token_source as hts

    hts.store_ha_admin_token("  " + REAL_SHAPED_TOKEN + "\n", sid)
    assert hts.resolve_ha_admin_token(sid).token == REAL_SHAPED_TOKEN


# -- 2. verify, against a fake Home Assistant ------------------------

class FakeResponse:
    def __init__(self, status):
        self.status_code = status
        self.content = b"{}"

    def json(self):
        return {}


class FakeSession:
    """Answers by URL suffix: ``/api/config`` then the admin probe."""

    config_status = 200
    probe_status = 404
    instances: list["FakeSession"] = []

    def __init__(self):
        self.headers: dict = {}
        self.calls: list[str] = []
        self.closed = False
        FakeSession.instances.append(self)

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url.endswith("/api/config"):
            return FakeResponse(FakeSession.config_status)
        return FakeResponse(FakeSession.probe_status)

    def post(self, url, **kwargs):            # pragma: no cover - never used
        raise AssertionError("verify must never write to Home Assistant")

    def close(self):
        self.closed = True

    @classmethod
    def reset(cls, config_status=200, probe_status=404):
        cls.instances = []
        cls.config_status = config_status
        cls.probe_status = probe_status


@pytest.fixture
def fake_ha(monkeypatch):
    import requests
    import puffpai.ha_provisioning as hp

    FakeSession.reset()
    monkeypatch.setattr(requests, "Session", FakeSession)
    monkeypatch.setattr(hp, "_ha_base_url", lambda: "http://ha.test")
    return hp


def test_verify_valid_admin(fake_ha):
    FakeSession.reset(200, 404)
    ha = fake_ha.verify_admin_credential(REAL_SHAPED_TOKEN)
    assert ha == {"reachable": True, "valid": True, "admin": True,
                  "status": 200}
    assert "administrator" in fake_ha.verify_message(ha)
    assert FakeSession.instances[0].closed is True


def test_verify_valid_but_not_admin(fake_ha):
    FakeSession.reset(200, 401)
    ha = fake_ha.verify_admin_credential(REAL_SHAPED_TOKEN)
    assert ha["valid"] is True and ha["admin"] is False
    assert "not an administrator" in fake_ha.verify_message(ha)


def test_verify_token_rejected(fake_ha):
    FakeSession.reset(401, 404)
    ha = fake_ha.verify_admin_credential(REAL_SHAPED_TOKEN)
    assert ha == {"reachable": True, "valid": False, "admin": None,
                  "status": 401}
    msg = fake_ha.verify_message(ha)
    assert "rejected by Home Assistant (401)" in msg
    # The probe must not run once the token is known to be worthless.
    assert FakeSession.instances[0].calls == ["http://ha.test/api/config"]


def test_verify_uses_a_throwaway_session_and_never_logs_the_token(
        fake_ha, caplog):
    with caplog.at_level(logging.DEBUG):
        FakeSession.reset(200, 404)
        fake_ha.verify_admin_credential(REAL_SHAPED_TOKEN)
    assert REAL_SHAPED_TOKEN not in caplog.text
    session = FakeSession.instances[0]
    assert session.headers["Authorization"].endswith(REAL_SHAPED_TOKEN)
    assert session.closed is True


def test_verify_unreachable_ha(fake_ha, monkeypatch):
    class Dead(FakeSession):
        def get(self, url, **kwargs):
            raise OSError("no route to host")

    import requests
    FakeSession.reset()
    monkeypatch.setattr(requests, "Session", Dead)
    ha = fake_ha.verify_admin_credential(REAL_SHAPED_TOKEN)
    assert ha == {"reachable": False, "valid": False, "admin": None,
                  "status": None}
    assert "did not answer" in fake_ha.verify_message(ha)


# -- 3. the routes ---------------------------------------------------

@pytest.fixture(scope="module")
def api_mod():
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules({"firewall_manager":
                          MagicMock(FirewallManager=lambda: mock_fw)}):
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")
        mod.app.config["TESTING"] = True
        yield mod


@pytest.fixture
def client(api_mod):
    api_mod._rate_limit_store.clear()
    api_mod._vault_sessions.clear()
    with api_mod.app.test_client() as c:
        yield c, api_mod._API_KEY, api_mod
    api_mod._vault_sessions.clear()


def _headers(api_key, session_token=None):
    h = {"X-API-Key": api_key, "Content-Type": "application/json"}
    if session_token:
        h["X-Vault-Token"] = session_token
    return h


def _unlocked(mod, monkeypatch, vault, sid, token="sess-tok"):
    """Wire a live vault session into the api_server session table."""
    monkeypatch.setattr(mod, "_get_vault", lambda: vault)
    mod._vault_sessions[token] = sid
    return token


@pytest.mark.parametrize("route", ["verify", "reveal"])
def test_both_routes_403_without_a_session(client, route):
    c, api_key, mod = client
    resp = c.post(f"/vault/secrets/ha_admin_token/{route}",
                  headers=_headers(api_key), json={"confirm": True})
    assert resp.status_code == 403
    assert "locked" in resp.get_json()["message"].lower()


def test_verify_route_reports_fingerprint_and_message(
        client, real_vault, fake_ha, monkeypatch):
    c, api_key, mod = client
    vault, sid = real_vault
    from puffpai.security import ha_token_source as hts
    hts.store_ha_admin_token(REAL_SHAPED_TOKEN, sid)
    tok = _unlocked(mod, monkeypatch, vault, sid)

    FakeSession.reset(200, 404)
    resp = c.post("/vault/secrets/ha_admin_token/verify",
                  headers=_headers(api_key, tok), json={})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["present"] is True
    assert body["fingerprint"] == {"length": len(REAL_SHAPED_TOKEN),
                                   "suffix": REAL_SHAPED_TOKEN[-4:]}
    assert body["ha"] == {"reachable": True, "valid": True, "admin": True,
                          "status": 200}
    assert body["message"].startswith("Stored token is valid")
    # The route is a checker, not an export path.
    assert REAL_SHAPED_TOKEN not in resp.get_data(as_text=True)


def test_verify_route_says_so_when_nothing_is_stored(
        client, real_vault, fake_ha, monkeypatch):
    c, api_key, mod = client
    vault, sid = real_vault
    tok = _unlocked(mod, monkeypatch, vault, sid)

    resp = c.post("/vault/secrets/ha_admin_token/verify",
                  headers=_headers(api_key, tok), json={})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["present"] is False
    assert body["fingerprint"] is None
    assert "No Home Assistant administrator credential" in body["message"]
    assert FakeSession.instances == []


def test_reveal_requires_confirm(client, real_vault, monkeypatch):
    c, api_key, mod = client
    vault, sid = real_vault
    from puffpai.security import ha_token_source as hts
    hts.store_ha_admin_token(REAL_SHAPED_TOKEN, sid)
    tok = _unlocked(mod, monkeypatch, vault, sid)

    resp = c.post("/vault/secrets/ha_admin_token/reveal",
                  headers=_headers(api_key, tok), json={})
    assert resp.status_code == 400
    assert REAL_SHAPED_TOKEN not in resp.get_data(as_text=True)


def test_reveal_returns_the_value_once_and_logs_no_value(
        client, real_vault, monkeypatch, caplog):
    c, api_key, mod = client
    vault, sid = real_vault
    from puffpai.security import ha_token_source as hts
    hts.store_ha_admin_token(REAL_SHAPED_TOKEN, sid)
    tok = _unlocked(mod, monkeypatch, vault, sid)

    with caplog.at_level(logging.DEBUG):
        resp = c.post("/vault/secrets/ha_admin_token/reveal",
                      headers=_headers(api_key, tok),
                      json={"confirm": True})
    assert resp.status_code == 200
    assert resp.get_json()["token"] == REAL_SHAPED_TOKEN
    assert REAL_SHAPED_TOKEN not in caplog.text
    assert "revealed to the owner" in caplog.text


def test_reveal_404_when_nothing_is_stored(client, real_vault, monkeypatch):
    c, api_key, mod = client
    vault, sid = real_vault
    tok = _unlocked(mod, monkeypatch, vault, sid)

    resp = c.post("/vault/secrets/ha_admin_token/reveal",
                  headers=_headers(api_key, tok), json={"confirm": True})
    assert resp.status_code == 404


def test_vault_item_route_still_refuses_the_credential(
        client, real_vault, monkeypatch):
    """The explicit reveal route does not soften the browsing route."""
    c, api_key, mod = client
    vault, sid = real_vault
    from puffpai.security import ha_token_source as hts
    hts.store_ha_admin_token(REAL_SHAPED_TOKEN, sid)
    tok = _unlocked(mod, monkeypatch, vault, sid)

    resp = c.get("/vault/item/ha_admin_token.json",
                 headers=_headers(api_key, tok))
    assert resp.status_code == 403
    assert REAL_SHAPED_TOKEN not in resp.get_data(as_text=True)
