"""A document that will not parse is never silently replaced by defaults.

`AppData._load` used to swallow `JSONDecodeError`/`IOError` and return the
app's default shape even for a file that exists and holds bytes. A corrupt or
half-written document then read as an EMPTY household: the merge path's drop
guard saw empty lists in `current` and had nothing to protect, and the next
page PUT wrote that emptiness over the owner's real file.

Now `AppDataCorrupt` is raised, the bytes are left exactly as they are, and
the REST routes answer 503 with "the app's data file is unreadable; nothing
was changed". Defaults are for a MISSING or EMPTY file only.

Real `AppData` on a temp data dir. Every row is invented.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

BROKEN = "{ this is the owner's broken file"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from puffpai import app_data as app_data_module
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_module, "_data_dir_cache",
                        str(tmp_path / "app_data"), raising=False)
    return app_data_module.AppData()


def _break(app, name="trivia"):
    path = Path(app._files[name])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BROKEN, encoding="utf-8")
    return path


# ── the store ───────────────────────────────────────────────────────────────

def test_a_corrupt_document_raises_instead_of_returning_defaults(app):
    from puffpai.app_data import AppDataCorrupt
    path = _break(app)
    with pytest.raises(AppDataCorrupt) as caught:
        app.get_data("trivia")
    assert caught.value.app == "trivia"
    assert path.read_text(encoding="utf-8") == BROKEN


def test_the_bytes_are_never_rewritten(app):
    from puffpai.app_data import AppDataCorrupt
    path = _break(app)
    for _ in range(3):
        with pytest.raises(AppDataCorrupt):
            app.get_data("trivia")
    assert path.read_text(encoding="utf-8") == BROKEN


def test_a_write_over_a_corrupt_document_is_refused_not_merged(app):
    """The actual data-loss path: a merge reads `current` first."""
    from puffpai.app_data import AppDataCorrupt
    path = _break(app, "freshtable")
    with pytest.raises(AppDataCorrupt):
        app.set_data("freshtable", {"pantry": []}, merge=True)
    assert path.read_text(encoding="utf-8") == BROKEN


def test_a_missing_file_still_gets_the_default_shape(app):
    path = Path(app._files["trivia"])
    assert not path.exists()
    data = app.get_data("trivia")
    assert data["results"] == []
    assert path.exists(), "first use did not bootstrap"


def test_an_empty_file_still_reads_as_the_default_shape(app):
    path = Path(app._files["trivia"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("   \n", encoding="utf-8")
    data = app.get_data("trivia")
    assert data["results"] == []
    assert path.read_text(encoding="utf-8") == "   \n", \
        "an empty file is not corrupt, but it is still the owner's"


def test_the_fault_is_logged_once_per_path(app, capsys):
    from puffpai import app_data as app_data_module
    from puffpai.app_data import AppDataCorrupt
    path = _break(app)
    app_data_module._corrupt_logged.discard(str(path))
    for _ in range(4):
        with pytest.raises(AppDataCorrupt):
            app.get_data("trivia")
    printed = capsys.readouterr().out
    assert printed.count("will not parse") == 1


def test_one_lock_per_document_shared_across_instances(tmp_path, monkeypatch):
    from puffpai import app_data as app_data_module
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_module, "_data_dir_cache",
                        str(tmp_path / "app_data"), raising=False)
    one = app_data_module.AppData()
    two = app_data_module.AppData()
    for name in ("calendar", "trivia", "freshtable"):
        assert one._locks[name] is two._locks[name], name


def test_the_save_path_is_the_atomic_secure_writer():
    """The app-data branch's writer, still in place under this change."""
    source = (REPO_ROOT / "src" / "puffpai" / "app_data.py").read_text(
        encoding="utf-8")
    assert "from puffpai.security.app_data_store import write_json_secure" in source
    assert "write_json_secure(self._files[app], data," in source


# ── the REST surface ────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def api():
    from unittest.mock import MagicMock
    from tests.modpatch import patched_modules

    src = str(REPO_ROOT / "src" / "puffpai")
    if src not in sys.path:
        sys.path.insert(0, src)
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules({"firewall_manager":
                          MagicMock(FirewallManager=lambda: mock_fw)}):
        import importlib
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")
        mod.app.config["TESTING"] = True
        yield mod


@pytest.fixture()
def corrupt_api(api, monkeypatch, tmp_path):
    """The live api_server, with one app's document unreadable on disk."""
    from puffpai.app_data import AppData
    from puffpai import app_data as app_data_module

    monkeypatch.setattr(app_data_module, "_data_dir_cache",
                        str(tmp_path / "app_data"), raising=False)
    store = AppData()
    path = Path(store._files["trivia"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BROKEN, encoding="utf-8")
    monkeypatch.setattr(api, "_get_app_data", lambda: store)
    return api, path


def test_a_read_of_a_corrupt_document_is_503(corrupt_api):
    api, path = corrupt_api
    with api.app.test_client() as c:
        response = c.get("/apps/trivia/data",
                         headers={"X-API-Key": api._API_KEY})
    assert response.status_code == 503
    assert response.get_json()["error"] == api.APP_DATA_UNREADABLE
    assert path.read_text(encoding="utf-8") == BROKEN


def test_a_write_to_a_corrupt_document_is_503_and_changes_nothing(corrupt_api):
    api, path = corrupt_api
    with api.app.test_client() as c:
        response = c.put("/apps/trivia/data", json={"results": []},
                         headers={"X-API-Key": api._API_KEY})
    assert response.status_code == 503
    assert response.get_json()["error"] == api.APP_DATA_UNREADABLE
    assert path.read_text(encoding="utf-8") == BROKEN


def test_an_action_on_a_corrupt_document_is_503(corrupt_api):
    api, path = corrupt_api
    with api.app.test_client() as c:
        response = c.post("/apps/trivia/action",
                          json={"action": "trivia_get_stats", "params": {}},
                          headers={"X-API-Key": api._API_KEY})
    assert response.status_code == 503
    assert path.read_text(encoding="utf-8") == BROKEN


def test_the_poll_route_is_503_rather_than_a_fresh_timestamp(corrupt_api):
    api, _ = corrupt_api
    with api.app.test_client() as c:
        response = c.get("/apps/trivia/updated",
                         headers={"X-API-Key": api._API_KEY})
    assert response.status_code == 503


def test_the_error_never_leaks_the_path(corrupt_api):
    api, path = corrupt_api
    with api.app.test_client() as c:
        body = c.get("/apps/trivia/data",
                     headers={"X-API-Key": api._API_KEY}).get_data(as_text=True)
    assert str(path) not in body
    assert "app_data" not in body


def test_the_chat_fastpath_says_so_instead_of_answering_from_defaults(
        corrupt_api):
    api, _ = corrupt_api
    reply = api._try_trivia_start("whats my quiz score")
    assert reply and "can't read the trivia file" in reply
    assert "Nothing was changed" in reply


def test_a_healthy_app_is_unaffected(corrupt_api):
    api, _ = corrupt_api
    with api.app.test_client() as c:
        response = c.get("/apps/calendar/data",
                         headers={"X-API-Key": api._API_KEY})
    assert response.status_code == 200
    assert "events" in response.get_json()



# ── the replace path, and the owner's escape hatch ──────────────────────────

def test_a_replace_over_a_corrupt_document_is_refused_too(app):
    """The replace path does not otherwise read the document, so without a
    check it is the ONE way to write over a file we could not decode."""
    from puffpai.app_data import AppDataCorrupt
    path = _break(app, "freshtable")
    with pytest.raises(AppDataCorrupt):
        app.set_data("freshtable", {"pantry": []}, merge=False)
    assert path.read_text(encoding="utf-8") == BROKEN


def test_recover_overwrites_on_the_replace_path(app):
    path = _break(app, "freshtable")
    assert app.set_data("freshtable", {"pantry": ["test-fake rice"]},
                        merge=False, recover=True) is True
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["pantry"] == ["test-fake rice"]


def test_recover_overwrites_on_the_merge_path(app):
    path = _break(app, "freshtable")
    assert app.set_data("freshtable", {"pantry": ["test-fake rice"]},
                        merge=True, recover=True) is True
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["pantry"] == ["test-fake rice"]


def test_a_replace_put_without_recover_is_503(corrupt_api):
    api, path = corrupt_api
    with api.app.test_client() as c:
        response = c.put("/apps/trivia/data?replace=1", json={"results": []},
                         headers={"X-API-Key": api._API_KEY})
    assert response.status_code == 503
    assert response.get_json()["error"] == api.APP_DATA_UNREADABLE
    assert path.read_text(encoding="utf-8") == BROKEN


def test_a_put_with_recover_1_is_allowed_through(corrupt_api):
    api, path = corrupt_api
    with api.app.test_client() as c:
        response = c.put("/apps/trivia/data?replace=1&recover=1",
                         json={"results": []},
                         headers={"X-API-Key": api._API_KEY})
    assert response.status_code == 200
    assert json.loads(path.read_text(encoding="utf-8"))["results"] == []


# ── publish order inside __init__ ───────────────────────────────────────────

def test_valid_apps_is_published_last(tmp_path, monkeypatch):
    """Every reader checks VALID_APPS before touching an app, so _files,
    _defaults and _locks must all be complete before it exists."""
    from puffpai import app_data as app_data_module

    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_module, "_data_dir_cache",
                        str(tmp_path / "app_data"), raising=False)
    seen = {}
    real_lock_for = app_data_module._lock_for

    def _watch(path):
        # Called while __init__ is building _locks: VALID_APPS must not be an
        # INSTANCE attribute yet, or another thread could already be inside.
        seen.setdefault("locks_built_before_valid_apps", True)
        return real_lock_for(path)

    monkeypatch.setattr(app_data_module, "_lock_for", _watch)
    store = app_data_module.AppData()
    assert seen.get("locks_built_before_valid_apps")
    assert set(store._locks) == set(store._files)
    assert set(store.VALID_APPS) == set(store._files)
    for app_id in store.VALID_APPS:
        assert app_id in store._locks, app_id
        assert app_id in store._defaults, app_id


def test_the_source_assigns_valid_apps_after_the_tables():
    source = (REPO_ROOT / "src" / "puffpai" / "app_data.py").read_text(
        encoding="utf-8")
    locks = source.index("self._locks = {app: _lock_for(path)")
    valid = source.index("self.VALID_APPS = list(self._files.keys())")
    assert locks < valid, "_locks must be built before VALID_APPS is published"
