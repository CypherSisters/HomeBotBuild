"""The /setup/* REST surface: who may call it, and what it answers.

Three properties, and they are the ones a regression would quietly break:

* **Nothing in the family is reachable without the app API key.** Not one route
  is in ``_PUBLIC_ENDPOINTS`` and not one is in
  ``endpoint_gate.LOCAL_ONLY_ENDPOINTS`` — so the default-deny classification
  covers them by construction rather than by a list somebody has to maintain.
* **The wizard closes.** Once ``setup_ops.is_complete()`` is true, a
  state-changing call needs a live vault session or gets 409 ``setup_complete``;
  reads stay open; the vault routes are refused outright.
* **The shapes are the ones the dashboard is coded against**
  (``dashboard/app/_lib/services/setupService.ts``).

Run:
    pytest tests/test_setup_api_routes.py
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from puffpai import setup_ops
from puffpai.config import get_config
from puffpai.security import endpoint_gate

LAN_PEER = "192.168.1.70"


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
    return api_server.app.test_client()


def _auth():
    from puffpai import api_server
    return {"Authorization": f"Bearer {api_server._API_KEY}"}


def _setup_rules():
    from puffpai import api_server
    return [r for r in api_server.app.url_map.iter_rules()
            if r.rule.startswith("/setup")]


#: Every route in the family, with the verbs that CHANGE something. Kept here
#: rather than derived, so adding a route without deciding its gate shows up as
#: a failure in `test_every_setup_route_is_classified`.
STATE_CHANGING = {
    ("POST", "/setup/identity"),
    ("POST", "/setup/posture"),
    ("POST", "/setup/bridge"),
    ("POST", "/setup/household"),
    ("POST", "/setup/storage"),
    ("POST", "/setup/ha"),
    ("POST", "/setup/ollama"),
    ("POST", "/setup/ollama/pull"),
    ("POST", "/setup/knowledge"),
    ("POST", "/setup/complete"),
}

READ_ONLY = {
    ("GET", "/setup/status"),
    ("GET", "/setup/defaults"),
    ("GET", "/setup/posture"),
    ("GET", "/setup/bridge"),
    ("GET", "/setup/household"),
    ("GET", "/setup/storage"),
    ("GET", "/setup/surfaces"),
    ("GET", "/setup/pending-apply"),
    ("GET", "/setup/hardware"),
    ("GET", "/setup/manifest"),
    ("GET", "/setup/ollama/pull"),
}


# ── 1. default-deny: the whole family is key-gated ──────────────────────────

class TestDefaultDeny:

    def test_every_setup_route_is_key_gated(self):
        """No /setup/* endpoint is unconditionally public or same-host exempt."""
        from puffpai import api_server
        for rule in _setup_rules():
            assert rule.endpoint not in api_server._PUBLIC_ENDPOINTS, (
                f"{rule.rule} ({rule.endpoint}) is answered to ANY caller")
            assert rule.endpoint not in endpoint_gate.LOCAL_ONLY_ENDPOINTS, (
                f"{rule.rule} ({rule.endpoint}) is answered to any same-host "
                "caller with no key")

    @pytest.mark.parametrize("method,path", sorted(STATE_CHANGING | READ_ONLY))
    def test_lan_peer_with_no_credential_gets_401(self, client, method, path):
        resp = client.open(path, method=method, json={},
                           environ_base={"REMOTE_ADDR": LAN_PEER})
        assert resp.status_code == 401, f"{method} {path}"

    @pytest.mark.parametrize("method,path", sorted(STATE_CHANGING | READ_ONLY))
    def test_loopback_with_no_credential_is_also_401(self, client, method, path):
        """Loopback buys nothing here: the setup family has no keyless caller."""
        resp = client.open(path, method=method, json={},
                           environ_base={"REMOTE_ADDR": "127.0.0.1"})
        assert resp.status_code == 401, f"{method} {path}"

    def test_vault_routes_are_401_without_the_key(self, client):
        for path in ("/setup/vault", "/setup/vault/confirm"):
            assert client.post(path, json={}).status_code == 401

    def test_every_setup_route_is_classified(self):
        """A new route must be given a gate, not inherit one by accident."""
        known = {p for _m, p in STATE_CHANGING | READ_ONLY}
        # The census-first + vault + probe routes are classified in their own
        # files; listed here so this test fails when something NEW appears.
        elsewhere = {"/setup/vault", "/setup/vault/confirm", "/setup/ha/test",
                     "/setup/brain-menu", "/setup/crew", "/setup/interview",
                     "/setup/hardware", "/setup/manifest", "/setup/surfaces"}
        mounted = {r.rule for r in _setup_rules()}
        assert mounted - known - elsewhere == set(), (
            "unclassified /setup route(s); decide the auth + completion gate "
            "and add them here")


# ── 2. the contract the dashboard is coded against ──────────────────────────

class TestShapes:

    def test_defaults_are_inlined_under_ok(self, client, scratch):
        body = client.get("/setup/defaults", headers=_auth()).get_json()
        assert body["ok"] is True
        assert body["bot_name"] == setup_ops.homebot_defaults()["bot_name"]
        assert body["persona"] == setup_ops.homebot_defaults()["persona"]

    def test_identity_persists_the_name(self, client, scratch):
        resp = client.post("/setup/identity", json={"bot_name": "Nimbus"},
                           headers=_auth())
        assert resp.status_code == 200
        assert resp.get_json()["bot_name"] == "Nimbus"
        assert get_config(_force_reload=True).identity.name == "Nimbus"

    def test_identity_refuses_an_empty_name(self, client, scratch):
        resp = client.post("/setup/identity", json={"bot_name": "  "},
                           headers=_auth())
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["ok"] is False and body["error"] == "bot_name_required"

    def test_identity_refuses_an_invented_persona(self, client, scratch):
        """This build ships one persona; a route may not pretend to store another."""
        resp = client.post("/setup/identity",
                           json={"bot_name": "Nimbus", "persona": "notapersona"},
                           headers=_auth())
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "unknown_persona"

    def test_posture_get_returns_both_menus(self, client, scratch):
        body = client.get("/setup/posture", headers=_auth()).get_json()
        assert body["ok"] is True
        for key in ("build_network", "run_egress", "egress_switches"):
            assert isinstance(body[key], list)

    def test_posture_post_persists_the_answer(self, client, scratch):
        body = client.post("/setup/posture",
                           json={"build_network": "offline",
                                 "run_egress": "sealed",
                                 "enabled_switches": []},
                           headers=_auth()).get_json()
        assert body["ok"] is True
        assert body["run_egress"] == "sealed"
        assert isinstance(body["gates"], dict)
        assert os.path.exists(body["config_path"])

    def test_household_round_trip(self, client, scratch):
        resp = client.post("/setup/household",
                           json={"owner_name": "Alex",
                                 "members": [{"name": "Sam"}]},
                           headers=_auth())
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["owner"] == "Alex"
        assert client.get("/setup/household",
                          headers=_auth()).get_json()["count"] == body["count"]

    def test_household_refuses_an_empty_owner(self, client, scratch):
        resp = client.post("/setup/household", json={"members": []},
                           headers=_auth())
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "owner_name_required"

    def test_household_ignores_a_caller_supplied_role(self, client, scratch):
        """A member cannot name itself owner through the setup route."""
        client.post("/setup/household",
                    json={"owner_name": "Alex",
                          "members": [{"name": "Mallory", "role": "owner"}]},
                    headers=_auth())
        assert client.get("/setup/household",
                          headers=_auth()).get_json()["owner"] == "Alex"

    def test_storage_get_offers_defaults(self, client, scratch):
        body = client.get("/setup/storage", headers=_auth()).get_json()
        assert body["ok"] is True
        assert set(body["paths"]) == set(setup_ops.storage_defaults())

    def test_storage_post_applies_and_reports_the_manifest_block(
            self, client, scratch):
        target = scratch / "chosen_state"
        body = client.post("/setup/storage",
                           json={"state_dir": str(target)},
                           headers=_auth()).get_json()
        assert body["ok"] is True
        assert os.path.isdir(target)
        assert body["manifest_block"]["state_dir"] == str(target)

    def test_storage_refuses_a_relative_path_and_writes_nothing(
            self, client, scratch):
        before = client.get("/setup/storage", headers=_auth()).get_json()["paths"]
        resp = client.post("/setup/storage", json={"state_dir": "somewhere"},
                           headers=_auth())
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["ok"] is False
        assert body["error"] == "invalid_storage_path"
        assert "state_dir" in body["problems"]
        after = client.get("/setup/storage", headers=_auth()).get_json()["paths"]
        assert after == before

    def test_surfaces_never_hands_back_an_empty_allowlist(self, client, scratch):
        body = client.get("/setup/surfaces", headers=_auth()).get_json()
        assert body["ok"] is True
        assert body["surfaces"], "an empty allowlist nobody asked for"
        assert isinstance(body["available"], list)

    def test_surfaces_applies_owner_edits(self, client, scratch):
        base = client.get("/setup/surfaces", headers=_auth()).get_json()
        victim = base["surfaces"][0]
        body = client.post("/setup/surfaces",
                           json={"goal": base["goal"], "remove": [victim]},
                           headers=_auth()).get_json()
        assert victim not in body["surfaces"]

    def test_bridge_get_returns_the_whole_ladder(self, client, scratch):
        body = client.get("/setup/bridge?home_assistant=1",
                          headers=_auth()).get_json()
        assert body["ok"] is True
        assert body["recommended"]
        assert len(body["postures"]) >= 4, "the ladder always comes back whole"

    def test_bridge_post_reconciles_and_records_the_edge(self, client, scratch):
        client.post("/setup/posture",
                    json={"run_egress": "sealed"}, headers=_auth())
        body = client.post("/setup/bridge",
                           json={"posture": "open_yard", "network_edge": True},
                           headers=_auth()).get_json()
        assert body["ok"] is True
        # Stricter wins: a sealed build does not sit behind Open Yard.
        assert body["posture"] != "open_yard"
        assert body["network_edge_recorded"] is True
        pending = client.get("/setup/pending-apply",
                             headers=_auth()).get_json()["pending"]
        # One name across both wizard faces: the CLI writes this kind too, and
        # record_pending_apply de-duplicates on it.
        assert any(item["kind"] == "zone3_network_edge" for item in pending)

    def test_bridge_post_refuses_an_unknown_posture(self, client, scratch):
        resp = client.post("/setup/bridge", json={"posture": "wide_open"},
                           headers=_auth())
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_bridge_post_records_a_sent_default_as_the_default(self, client,
                                                               scratch):
        """The HomeBot browser setup sends the build's default (Moat) with
        nobody picking it: recorded as the default, never as the owner's choice."""
        body = client.post("/setup/bridge",
                           json={"posture": "moat", "network_edge": False,
                                 "defaulted": True},
                           headers=_auth()).get_json()
        assert body["ok"] is True
        assert body["bridge"]["chosen_by"] == "default"
        picked = client.post("/setup/bridge",
                             json={"posture": "moat", "network_edge": False},
                             headers=_auth()).get_json()
        assert picked["bridge"]["chosen_by"] == "owner"

    def test_pending_apply_is_empty_on_a_fresh_build(self, client, scratch):
        body = client.get("/setup/pending-apply", headers=_auth()).get_json()
        assert body == {"ok": True, "pending": []}


# ── 3. Home Assistant stays inside the house ────────────────────────────────

class TestHomeAssistantBoundary:

    def test_absence_is_a_complete_answer(self, client, scratch):
        body = client.post("/setup/ha", json={"present": False},
                           headers=_auth()).get_json()
        assert body["ok"] is True
        assert body["configured"] is False
        assert body["ha"]["present"] is False

    def test_a_public_address_is_refused_and_no_token_is_written(
            self, client, scratch):
        resp = client.post("/setup/ha",
                           json={"present": True, "url": "http://8.8.8.8:8123",
                                 "token": "tok-should-never-land"},
                           headers=_auth())
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "ha_not_local"
        assert not os.path.exists(setup_ops._ha_config_path())

    def test_the_probe_route_refuses_a_public_address_too(self, client, scratch):
        """/setup/ha/test must not be an SSRF primitive for a key holder."""
        resp = client.post("/setup/ha/test",
                           json={"url": "http://93.184.216.34:8123",
                                 "token": "tok-should-never-leave"},
                           headers=_auth())
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "ha_not_local"

    def test_a_house_local_address_is_accepted(self, client, scratch):
        resp = client.post("/setup/ha",
                           json={"present": True,
                                 "url": "http://192.168.7.50:8123",
                                 "token": "tok-lan-only"},
                           headers=_auth())
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ha"]["token_configured"] is True
        # The token is recorded as PLACED, never echoed.
        assert "tok-lan-only" not in resp.get_data(as_text=True)

    def test_credential_responses_are_no_store(self, client, scratch):
        resp = client.post("/setup/ha",
                           json={"present": True,
                                 "url": "http://192.168.7.50:8123",
                                 "token": "tok-lan-only"},
                           headers=_auth())
        assert "no-store" in resp.headers.get("Cache-Control", "")


# ── 4. the wizard closes ────────────────────────────────────────────────────

class TestPostCompletionLock:

    @pytest.mark.parametrize("method,path", sorted(STATE_CHANGING))
    def test_state_changing_calls_are_409_after_completion(
            self, client, scratch, method, path):
        setup_ops.mark_complete()
        resp = client.open(path, method=method, json={}, headers=_auth())
        assert resp.status_code == 409, f"{method} {path}"
        body = resp.get_json()
        assert body["ok"] is False
        assert body["error"] == "setup_complete"

    @pytest.mark.parametrize("method,path", sorted(READ_ONLY))
    def test_reads_stay_open_after_completion(self, client, scratch,
                                              method, path):
        setup_ops.mark_complete()
        resp = client.open(path, method=method, headers=_auth())
        assert resp.status_code == 200, f"{method} {path}"

    def test_manifest_preview_is_open_but_commit_is_gated(self, client, scratch,
                                                          monkeypatch):
        monkeypatch.setattr(setup_ops, "list_models", lambda *a, **k: [])
        setup_ops.mark_complete()
        preview = client.post("/setup/manifest", json={"stated_ram": 64},
                              headers=_auth())
        assert preview.status_code == 200
        assert preview.get_json()["written"] is False
        commit = client.post("/setup/manifest",
                             json={"stated_ram": 64, "commit": True},
                             headers=_auth())
        assert commit.status_code == 409
        assert commit.get_json()["error"] == "setup_complete"

    def test_an_unreadable_completion_flag_fails_closed(self, client, scratch,
                                                        monkeypatch):
        def _boom():
            raise OSError("flag unreadable")
        monkeypatch.setattr(setup_ops, "is_complete", _boom)
        resp = client.post("/setup/identity", json={"bot_name": "Nimbus"},
                           headers=_auth())
        assert resp.status_code == 409

    def test_a_live_vault_session_reopens_the_non_vault_routes(
            self, client, scratch):
        from puffpai import api_server

        setup_ops.seal_vault_with_recovery("vault-pin-owner-1")
        unlock = client.post("/vault/unlock",
                             json={"pin": "vault-pin-owner-1"},
                             headers=_auth())
        assert unlock.status_code == 200, unlock.get_data(as_text=True)
        token = unlock.get_json()["session_token"]
        setup_ops.mark_complete()

        headers = dict(_auth())
        headers["X-Vault-Token"] = token
        resp = client.post("/setup/identity", json={"bot_name": "Nimbus"},
                           headers=headers)
        assert resp.status_code == 200
        # ...and a bogus token does not.
        headers["X-Vault-Token"] = "not-a-session"
        assert client.post("/setup/identity", json={"bot_name": "Nope"},
                           headers=headers).status_code == 409
        api_server._vault_sessions.pop(token, None)
