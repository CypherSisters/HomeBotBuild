"""No secret the setup surface handles reaches a log, a manifest, or the feed.

Three values pass through /setup/*: the vault PIN, the recovery code, and the
Home Assistant long-lived token. Each is a value the household cannot re-issue
from anything the box keeps (the code) or that grants control of the house (the
token), and log files outlive the screens that showed them — ``start_api.py``
writes a rotating file at INFO, which carries LLM reply fragments and uploaded
filenames, so "it is only INFO" is not an answer.

This file drives the real routes with marked secrets and then GREPS everything
the run captured: the ``puffpai`` logger, root logging, stdout/stderr, the
response bodies, the build manifest, and the agent-activity feed.

Run:
    pytest tests/test_setup_api_secrets.py
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import pytest

from puffpai import setup_ops
from puffpai.config import get_config

#: Distinctive enough that a substring match cannot be a coincidence.
MARK_PIN = "MARKERpin-Zq7-setup-secret"
MARK_TOKEN = "MARKERtoken-Zq7-setup-secret"


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


@pytest.fixture
def captured(caplog):
    """Capture the `puffpai` logger too, not just whatever propagates."""
    caplog.set_level(logging.DEBUG)
    puff = logging.getLogger("puffpai")
    previous = puff.propagate
    puff.propagate = True
    puff.addHandler(caplog.handler)
    puff.setLevel(logging.DEBUG)
    try:
        yield caplog
    finally:
        puff.removeHandler(caplog.handler)
        puff.propagate = previous


def _all_captured_text(caplog, capsys):
    out, err = capsys.readouterr()
    lines = [caplog.text, out, err]
    for record in caplog.records:
        lines.append(str(record.getMessage()))
        lines.append(str(getattr(record, "args", "")))
    return "\n".join(lines)


def _walk_files(root):
    for base, _dirs, names in os.walk(root):
        for name in names:
            yield os.path.join(base, name)


class TestSecretsNeverLeak:

    def test_vault_pin_and_recovery_code_stay_out_of_everything(
            self, client, scratch, captured, capsys):
        resp = client.post("/setup/vault", json={"pin": MARK_PIN},
                           headers=_auth())
        assert resp.status_code == 200, resp.get_data(as_text=True)
        code = resp.get_json()["recovery_code"]
        bare_code = code.replace("-", "")

        # A wrong answer, then the right one - both log paths exercised.
        client.post("/setup/vault/confirm", json={"recovery_code": "NOPE"},
                    headers=_auth())
        client.post("/setup/vault/confirm", json={"recovery_code": code},
                    headers=_auth())

        logged = _all_captured_text(captured, capsys)
        assert MARK_PIN not in logged, "the vault PIN reached a log line"
        assert code not in logged, "the recovery code reached a log line"
        assert bare_code not in logged

        # ...and nothing on disk under this install holds either one.
        for path in _walk_files(scratch):
            try:
                blob = open(path, "rb").read()
            except OSError:
                continue
            assert MARK_PIN.encode() not in blob, f"PIN found in {path}"
            assert code.encode() not in blob, f"recovery code found in {path}"
            assert bare_code.encode() not in blob, f"recovery code found in {path}"

    def test_a_weak_pin_refusal_does_not_echo_the_pin(self, client, scratch,
                                                      captured, capsys):
        weak = "abc"
        resp = client.post("/setup/vault", json={"pin": weak}, headers=_auth())
        assert resp.status_code == 400
        assert weak not in resp.get_json()["message"]
        assert weak not in _all_captured_text(captured, capsys).replace(
            "abcdefghijklmnopqrstuvwxyz", "")

    def test_ha_token_stays_out_of_logs_and_the_manifest(
            self, client, scratch, captured, capsys, monkeypatch):
        monkeypatch.setattr(setup_ops, "list_models", lambda *a, **k: [])
        saved = client.post("/setup/ha",
                            json={"present": True,
                                  "url": "http://192.168.7.51:8123",
                                  "token": MARK_TOKEN},
                            headers=_auth())
        assert saved.status_code == 200
        assert MARK_TOKEN not in saved.get_data(as_text=True)

        # A refused probe must not log the token either.
        client.post("/setup/ha/test",
                    json={"url": "http://8.8.8.8:8123", "token": MARK_TOKEN},
                    headers=_auth())

        logged = _all_captured_text(captured, capsys)
        assert MARK_TOKEN not in logged, "the HA token reached a log line"

        committed = client.post("/setup/manifest",
                                json={"stated_ram": 64, "commit": True},
                                headers=_auth())
        assert committed.status_code == 200
        assert MARK_TOKEN not in committed.get_data(as_text=True)

        # The token lives in exactly ONE place: the gitignored HA config file.
        holders = []
        for path in _walk_files(scratch):
            try:
                blob = open(path, "rb").read()
            except OSError:
                continue
            if MARK_TOKEN.encode() in blob:
                holders.append(os.path.basename(path))
        assert holders == ["home_assistant_config.json"], holders

    def test_no_secret_reaches_the_agent_activity_feed(self, client, scratch,
                                                       captured, capsys):
        from puffpai import agent_activity, agent_events

        resp = client.post("/setup/vault", json={"pin": MARK_PIN},
                           headers=_auth())
        code = resp.get_json()["recovery_code"]
        client.post("/setup/ha",
                    json={"present": True, "url": "http://192.168.7.52:8123",
                          "token": MARK_TOKEN},
                    headers=_auth())

        agent_activity.reset_cache()
        # FILES only, and only the feed's own files. `agent_logs_dir()` can
        # resolve to the install root on a scratch install, and sweeping that
        # would re-read home_assistant_config.json - which is exactly where the
        # token is SUPPOSED to be (proved above), so it would assert nothing.
        feeds = [agent_activity.get_activity_log_path()]
        for name in ("event_log_path", "get_event_log_path"):
            resolver = getattr(agent_events, name, None)
            if callable(resolver):
                try:
                    feeds.append(resolver())
                except Exception:
                    pass
        blob = b""
        for target in feeds:
            if target and os.path.isfile(target):
                try:
                    blob += open(target, "rb").read()
                except OSError:
                    pass
        for secret in (MARK_PIN, MARK_TOKEN, code, code.replace("-", "")):
            assert secret.encode() not in blob
