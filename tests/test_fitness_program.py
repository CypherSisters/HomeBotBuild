"""The /fitness/program HTTP route + the owner's profile.

D9 was: the route 404s with "no profiles configured" because no profile
exists. This proves that with a profile in the store the route returns 200,
and that the shipped owner profile (config/profiles.json) parses and drives it.

The route calls ``profile_manager.profiles_path()`` *inside* the handler; the
accessor honours an explicitly assigned ``_PROFILES_PATH`` module global as an
override. profile_manager's docstring is explicit that ``monkeypatch.setattr``
on that name corrupts the module for the rest of the session; the sanctioned
override is ``monkeypatch.setitem(vars(mod), ...)``, which deletes the name
again on teardown. This test uses that form.
"""

import json
import os
import sys

import pytest
from unittest.mock import MagicMock

from tests.modpatch import patched_modules

_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)


@pytest.fixture(scope="module")
def api_app():
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules({"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        import importlib
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")
        mod.app.config["TESTING"] = True
        yield mod.app, mod._API_KEY, mod


@pytest.fixture()
def client(api_app):
    app, api_key, mod = api_app
    mod._rate_limit_store.clear()
    with app.test_client() as c:
        yield c, {"X-API-Key": api_key}, mod


def _write_profiles(tmp_path, monkeypatch, profiles):
    """Point the route's profile store at a temp file (sanctioned override)."""
    import puffpai.profile_manager as pm
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(profiles), encoding="utf-8")
    monkeypatch.setitem(vars(pm), "_PROFILES_PATH", str(path))
    return str(path)


def _owner_profiles():
    return {
        "profiles": [{
            "id": "owner",
            "display_name": "Owner",
            "preferences": {"workout_level": "intermediate"},
            "prefs": {"level": "intermediate", "program": "readiness_minimalist",
                      "duration": 45},
        }],
        "last_active_profile": "owner",
    }


def test_no_profile_still_404s(client, tmp_path, monkeypatch):
    """The D9 symptom — unchanged when the store is genuinely empty."""
    c, headers, _ = client
    _write_profiles(tmp_path, monkeypatch, {"profiles": []})
    resp = c.get("/fitness/program", headers=headers)
    assert resp.status_code == 404
    assert "no profiles configured" in resp.get_json()["error"].lower()


def test_owner_profile_makes_the_route_return_200(client, tmp_path, monkeypatch):
    c, headers, _ = client
    _write_profiles(tmp_path, monkeypatch, _owner_profiles())
    resp = c.get("/fitness/program?user=owner", headers=headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["user"] == "Owner"
    assert body["program"]           # a title string
    assert body["session"] is not None
    assert body["session"]["exercises"]


def test_shipped_owner_profile_file_drives_the_route(client, tmp_path, monkeypatch):
    """The real config/profiles.json we ship parses and yields a 200."""
    c, headers, _ = client
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    shipped = os.path.join(repo, "config", "profiles.json")
    with open(shipped, encoding="utf-8") as f:
        profiles = json.load(f)
    assert profiles["profiles"], "shipped profile file must not be empty"
    _write_profiles(tmp_path, monkeypatch, profiles)
    resp = c.get("/fitness/program", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["session"] is not None
