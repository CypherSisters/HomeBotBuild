"""After first run, the build surface needs an unlocked vault, everywhere.

THE HOLE. ``/setup/*`` refused state changes after completion (409 unless a
vault session is unlocked), but the card wizard and the persona writer change
the same build under other names and answered to the API key alone. From any
paired device, ``POST /wizard/apply`` renamed the bot and rewrote the Bridge
posture on a finished box.

Pinned here:
  * after completion, every WRITE under /wizard and /persona gets exactly what
    /setup/* gets (``_setup_gate``), and succeeds with an unlocked vault;
  * reads (and the write-nothing ``POST /persona/preview``) stay open;
  * before completion, the first-run rules are unchanged, and /persona writes
    now need the first-run code like the rest of the build surface;
  * the first-run code is retired on EVERY completion path;
  * ``/setup/ha/test`` is gated the same way after completion and never
    follows a redirect.

No network, no real state directory.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

REMOTE = {"REMOTE_ADDR": "192.168.1.66"}
LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}
GOOD_TOKEN = "unlocked-session-token-for-tests"
GOOD_SID = "vault-sid-ok"


@pytest.fixture
def scratch(tmp_path):
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
    api_server._vault_sessions.clear()
    api_server.app.config.update(TESTING=True)
    yield api_server.app.test_client()
    api_server._vault_sessions.clear()


@pytest.fixture
def complete(scratch):
    from puffpai import setup_ops
    setup_ops.mark_complete()
    assert setup_ops.is_complete()


class _UnlockedVault:
    """Stands in for the vault object: exactly one session id is valid."""

    def _validate_session(self, sid):
        if sid != GOOD_SID:
            raise PermissionError("locked")


@pytest.fixture
def unlocked(client, monkeypatch):
    from puffpai import api_server
    monkeypatch.setattr(api_server, "_get_vault", lambda: _UnlockedVault())
    api_server._vault_sessions[GOOD_TOKEN] = GOOD_SID
    return {"X-Vault-Token": GOOD_TOKEN}


def _auth(extra=None):
    from puffpai import api_server
    headers = {"Authorization": f"Bearer {api_server._API_KEY}"}
    headers.update(extra or {})
    return headers


def _config_text(scratch) -> str:
    out = []
    for path in Path(scratch).rglob("*.json"):
        try:
            out.append(path.read_text(encoding="utf-8"))
        except OSError:
            pass
    return "\n".join(out)


RENAME = {"skill": "purpose:identity", "answers": {"name": "Pwned"}}

WRITES = [
    ("POST", "/wizard/apply", RENAME),
    ("POST", "/wizard/apply",
     {"skill": "secure:bridge", "answers": {"bridge_posture": "open_yard"}}),
    ("POST", "/wizard/run", {"acknowledged": []}),
    ("POST", "/wizard/resume", {"choice": "restart"}),
    ("POST", "/persona/scaffold", {}),
    ("PATCH", "/persona/somebody", {}),
    ("DELETE", "/persona/somebody", {"confirm": "somebody"}),
]


class TestAfterCompletionWithoutAVault:
    @pytest.mark.parametrize("method,path,body", WRITES)
    @pytest.mark.parametrize("env", [REMOTE, LOOPBACK])
    def test_every_write_gets_what_setup_gets(self, client, complete,
                                              method, path, body, env):
        reference = client.post("/setup/identity", json={"bot_name": "Pwned2"},
                                headers=_auth(), environ_base=env)
        resp = client.open(path, method=method, json=body, headers=_auth(),
                           environ_base=env)
        assert reference.status_code == 409
        assert resp.status_code == reference.status_code, resp.get_data(as_text=True)
        assert resp.get_json()["error"] == "setup_complete"

    def test_a_remote_rename_changes_nothing(self, client, complete, scratch):
        client.post("/wizard/apply", json=RENAME, headers=_auth(),
                    environ_base=REMOTE)
        assert "Pwned" not in _config_text(scratch)

    def test_a_token_that_is_not_a_live_session_does_not_count(
            self, client, complete, unlocked):
        resp = client.post("/wizard/apply", json=RENAME,
                           headers=_auth({"X-Vault-Token": "made-up"}),
                           environ_base=REMOTE)
        assert resp.status_code == 409

    @pytest.mark.parametrize("path", ["/wizard/cards", "/wizard/state",
                                      "/persona/list"])
    def test_reads_stay_open(self, client, complete, path):
        resp = client.get(path, headers=_auth(), environ_base=REMOTE)
        assert resp.status_code != 409
        assert resp.status_code < 500

    def test_the_write_nothing_preview_stays_open(self, client, complete):
        resp = client.post("/persona/preview", json={}, headers=_auth(),
                           environ_base=REMOTE)
        assert (resp.get_json() or {}).get("error") != "setup_complete"


class TestAfterCompletionWithAnUnlockedVault:
    def test_the_owner_can_still_change_the_build(self, client, complete,
                                                  unlocked, scratch):
        resp = client.post("/wizard/apply", json=RENAME,
                           headers=_auth(unlocked), environ_base=REMOTE)
        assert resp.status_code == 200, resp.get_data(as_text=True)

    @pytest.mark.parametrize("method,path,body", WRITES)
    def test_no_write_is_refused_as_setup_complete(self, client, complete,
                                                   unlocked, method, path,
                                                   body):
        resp = client.open(path, method=method, json=body,
                           headers=_auth(unlocked), environ_base=REMOTE)
        assert (resp.get_json() or {}).get("error") != "setup_complete"


class TestBeforeCompletion:
    def test_first_run_rules_are_unchanged_for_the_local_wizard(self, client,
                                                                scratch):
        resp = client.post("/wizard/apply", json=RENAME, headers=_auth(),
                           environ_base=LOOPBACK)
        assert resp.status_code == 200, resp.get_data(as_text=True)

    @pytest.mark.parametrize("method,path", [("POST", "/persona/scaffold"),
                                             ("PATCH", "/persona/somebody"),
                                             ("DELETE", "/persona/somebody")])
    def test_persona_writes_need_the_first_run_code(self, client, scratch,
                                                    method, path):
        from puffpai.security import setup_code
        assert setup_code.current()
        resp = client.open(path, method=method, json={}, headers=_auth(),
                           environ_base=REMOTE)
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "setup_code_required"

    def test_the_kiosk_persona_list_stays_open(self, client, scratch):
        from puffpai.security import setup_code
        assert setup_code.current()
        resp = client.get("/persona/list", headers=_auth(), environ_base=REMOTE)
        assert resp.status_code != 403


class TestTheCodeIsRetiredOnEveryCompletionPath:
    def test_mark_complete_removes_the_code(self, scratch):
        from puffpai import setup_ops
        from puffpai.security import setup_code
        assert setup_code.current()
        assert os.path.exists(setup_code._state_path())
        setup_ops.mark_complete()
        assert not os.path.exists(setup_code._state_path())
        assert setup_code.current() is None

    @pytest.mark.parametrize("relpath", ["setup_cli.py", "wizard/host.py",
                                         "api_server.py"])
    def test_every_completion_path_goes_through_mark_complete(self, relpath):
        src = Path(__file__).resolve().parents[1] / "src" / "puffpai" / relpath
        text = src.read_text(encoding="utf-8")
        assert "setup_ops.mark_complete()" in text
        # Nobody writes the completion flag by hand beside it.
        assert "_setup_flag_path()" not in text

    def test_the_complete_route_retires_it_too(self, client, scratch):
        from puffpai.security import setup_code
        assert setup_code.current()
        resp = client.post("/setup/complete", json={}, headers=_auth(),
                           environ_base=LOOPBACK)
        assert resp.status_code == 200
        assert not os.path.exists(setup_code._state_path())


class TestTheHaProbe:
    def test_it_is_gated_after_completion(self, client, complete):
        resp = client.post("/setup/ha/test",
                           json={"url": "http://127.0.0.1:22", "token": "t"},
                           headers=_auth(), environ_base=REMOTE)
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "setup_complete"

    def test_it_never_follows_a_redirect(self, monkeypatch):
        from puffpai import setup_ops
        seen = {}

        class _Resp:
            status_code = 302
            ok = True

            def json(self):
                return {"version": "2026.9"}

        def fake_get(url, **kwargs):
            seen.update(kwargs)
            return _Resp()

        monkeypatch.setattr(setup_ops.requests, "get", fake_get)
        result = setup_ops.test_ha_connection("http://192.168.1.20:8123", "t")
        assert seen.get("allow_redirects") is False
        assert result["ok"] is False
        assert "redirect" in result["error"].lower()
        assert "t" != result["error"]
