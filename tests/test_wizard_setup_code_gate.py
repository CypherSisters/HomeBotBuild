"""The wizard is not open to the LAN either.

THE HOLE THIS PINS. ``tests/test_setup_code_gate.py`` closed ``/setup/*`` with
a console-only first-run code. The rest of the build surface answers under
different names and was NOT covered:

  * ``/wizard/*``  -- the card renderer the dashboard drives. ``/wizard/apply``
    writes config, ``/wizard/run`` builds, ``/wizard/resume`` decides what a
    stopped run may skip;
  * ``/build/prove*`` -- runs proofs against the running build and rewrites the
    recorded proof.

Both answered with the appliance's API key alone, and the dashboard's proxy
attaches that key server-side for any client that can load the page, at the
shipped ``PUFF_PAIRING_ENFORCE=knowledge`` default. So the first device on the
network could drive the build through a door with a different name.

THE CONTROL, pinned here: while ``setup_code.is_required()`` the whole family is
refused (every method, reads included) for a client that is not on this machine;
loopback callers are unaffected; once setup completes the gate stands down and
the routes are API-key-gated exactly as before.

No network, no real state directory (every test gets its own tmp_path), no
Ollama: only routes that read local state are exercised.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

#: A LAN peer. Flask's test client defaults to 127.0.0.1, so a test that wants
#: to look remote has to say so with environ_base.
REMOTE = {"REMOTE_ADDR": "192.168.1.66"}
LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

#: Read-shaped routes in the family that touch local state only.
READ_ROUTES = ("/wizard/state", "/build/prove")
#: Write-shaped routes in the family.
WRITE_ROUTES = ("/wizard/apply", "/wizard/run", "/wizard/resume",
                "/build/prove/owner")


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
    api_server._vault_sessions.clear()
    api_server.app.config.update(TESTING=True)
    return api_server.app.test_client()


@pytest.fixture
def code(scratch):
    from puffpai.security import setup_code
    value = setup_code.current()
    assert value, "setup should be incomplete in a fresh scratch install"
    return value


def _auth():
    from puffpai import api_server
    return {"Authorization": f"Bearer {api_server._API_KEY}"}


def _headers(setup=None):
    headers = dict(_auth())
    if setup is not None:
        headers["X-Setup-Code"] = setup
    return headers


class TestTheWizardFamilyIsRefusedWithoutTheCode:
    @pytest.mark.parametrize("path", READ_ROUTES)
    def test_a_lan_read_without_the_code_is_refused(self, client, code, path):
        """Reads are gated here, unlike /setup/* : GET /wizard/state and GET
        /build/prove describe the machine and what is still open in the build,
        and no off-box client has a bootstrap reason to read them."""
        resp = client.get(path, headers=_auth(), environ_base=REMOTE)
        assert resp.status_code == 403, f"{path} -> {resp.status_code}"
        body = resp.get_json()
        assert body["error"] == "setup_code_required", path
        assert body["setup_code_required"] is True
        assert code not in resp.get_data(as_text=True)

    @pytest.mark.parametrize("path", WRITE_ROUTES)
    def test_a_lan_write_without_the_code_is_refused(self, client, code, path):
        resp = client.post(path, json={}, headers=_auth(), environ_base=REMOTE)
        assert resp.status_code == 403, f"{path} -> {resp.status_code}"
        assert resp.get_json()["error"] == "setup_code_required", path

    def test_a_wrong_code_is_refused_too(self, client, code):
        resp = client.get("/wizard/state", headers=_headers(setup="ZZZZ-ZZZZ"),
                          environ_base=REMOTE)
        assert resp.status_code == 403
        assert resp.get_json()["reason"] == "bad_code"

    def test_no_route_under_the_family_escapes_the_gate(self, client, code):
        """Enumerated from the url map, so a route added tomorrow is covered
        without anybody remembering to cover it."""
        from puffpai import api_server
        checked = 0
        for rule in api_server.app.url_map.iter_rules():
            path = str(rule.rule)
            if "<" in path:
                continue
            if not api_server._under_prefix(
                    path, ("/wizard", "/build/prove", "/build/self")):
                continue
            for method in ("GET", "POST"):
                if method not in (rule.methods or set()):
                    continue
                call = client.get if method == "GET" else client.post
                kwargs = {} if method == "GET" else {"json": {}}
                resp = call(path, headers=_auth(), environ_base=REMOTE,
                            **kwargs)
                assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}"
                assert resp.get_json()["error"] == "setup_code_required", path
                checked += 1
        assert checked >= 6, f"only {checked} wizard/prove routes were found"


class TestTheCodeLetsTheOwnerThrough:
    @pytest.mark.parametrize("path", READ_ROUTES)
    def test_a_lan_read_with_the_code_is_allowed(self, client, code, path):
        resp = client.get(path, headers=_headers(setup=code),
                          environ_base=REMOTE)
        assert resp.status_code == 200, resp.get_data(as_text=True)

    def test_a_lan_write_with_the_code_reaches_the_route(self, client, code):
        """The route's own validation answers, not the gate: an empty body is a
        400/409 from the handler, and never a 403 from the code check."""
        resp = client.post("/wizard/apply", json={},
                           headers=_headers(setup=code), environ_base=REMOTE)
        assert resp.status_code != 403, resp.get_data(as_text=True)
        assert (resp.get_json() or {}).get("error") != "setup_code_required"

    @pytest.mark.parametrize("path", READ_ROUTES)
    def test_a_loopback_client_needs_no_code(self, client, code, path):
        """`puffpai prove` and the CLI wizard talk to this backend from the
        machine itself; the code is a proof of console access they already
        have."""
        resp = client.get(path, headers=_auth(), environ_base=LOOPBACK)
        assert resp.status_code == 200, resp.get_data(as_text=True)

    def test_the_api_key_is_still_required(self, client, code):
        """The setup code is a SECOND factor, never a replacement: no key, no
        entry, even with a perfect code."""
        resp = client.get("/wizard/state", headers={"X-Setup-Code": code},
                          environ_base=REMOTE)
        assert resp.status_code in (401, 403)
        assert resp.get_json().get("error") != "ok"


class TestItStandsDownWhenSetupIsDone:
    def test_after_completion_the_family_is_key_gated_as_before(
            self, client, code, scratch):
        from puffpai.security import setup_code as sc
        done = client.post("/setup/complete", headers=_headers(setup=code),
                           environ_base=REMOTE)
        assert done.status_code == 200, done.get_json()
        assert sc.is_required() is False

        allowed = client.get("/wizard/state", headers=_auth(),
                             environ_base=REMOTE)
        assert allowed.status_code == 200, allowed.get_data(as_text=True)

        keyless = client.get("/wizard/state", environ_base=REMOTE)
        assert keyless.status_code == 401


class TestThePrefixMatchIsAnchored:
    @pytest.mark.parametrize("path,expected", [
        ("/wizard", True),
        ("/wizard/run", True),
        ("/build/prove", True),
        ("/build/prove/live", True),
        ("/build/self", True),
        ("/buildings", False),
        ("/wizardry/run", False),
        ("/build/proven", False),
        ("/chat", False),
        ("", False),
    ])
    def test_a_longer_word_is_not_the_prefix(self, path, expected):
        from puffpai import api_server
        assert api_server._under_prefix(
            path, api_server._SETUP_CODE_PREFIXES) is expected, path
