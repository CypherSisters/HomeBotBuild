"""An app installed by ANOTHER process is live on the next read.

``docs/EXTENDING.md`` says a scaffolded persona works "live immediately". It did
not, across a process boundary: ``puffpai persona new`` is a separate process, so
it wrote ``<data>/apps/<id>/app.json`` and rescanned ITS OWN copy of
``app_registry._cached_manifests`` — a module-level cache — while the running
server kept serving the list it had. ``GET /apps/installed`` omitted the persona
and ``GET /apps/<id>/data`` answered 404 until somebody called
``POST /apps/rescan`` or restarted the service.

Every test here simulates the second process the only honest way: by writing the
app folder straight to disk and then calling the routes, WITHOUT any rescan call
in between. A test that called ``rescan()`` first would be testing the mechanism
that already worked.

No network, no model, no Orin.

Run:
    py -3 -m pytest tests/test_app_registry_staleness.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import api_server                       # noqa: E402
from puffpai import app_data as app_data_mod         # noqa: E402
from puffpai import app_registry                     # noqa: E402
from puffpai import persona_scaffold                 # noqa: E402

AUTH = {"X-API-Key": api_server._API_KEY}


@pytest.fixture(autouse=True)
def _fresh_registry():
    """The manifest cache is process-wide; nothing written here outlives its own
    test, or it becomes a stray app in whatever file runs next."""
    app_registry.rescan()
    api_server._reset_app_data()
    api_server._rate_limit_store.clear()
    yield
    app_registry.rescan()
    api_server._reset_app_data()


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A client over a build whose owner roots are in tmp_path.

    ``$PUFFPAI_DATA_DIR`` is what :func:`app_registry.user_apps_dir` resolves
    through; the store dir is redirected too, because an app's document must
    never land in the checkout.
    """
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_mod, "_data_dir",
                        lambda: str(tmp_path / "app_data"))
    app_registry.rescan()
    api_server._reset_app_data()
    api_server.app.config["TESTING"] = True
    return api_server.app.test_client()


def installed_ids(client) -> list:
    """The app ids /apps/installed reports (it returns full ui records)."""
    payload = client.get('/apps/installed', headers=AUTH).get_json()
    return [record['id'] for record in payload['apps']]


def install_from_another_process(app_id: str, *, name: str = "",
                                 lists=("notes",)) -> Path:
    """Write a valid app + pack the way a second process would: files only.

    Uses the SAME manifest builders ``puffpai persona new`` uses, so what lands
    on disk is byte-for-byte what the CLI writes — but deliberately NOT
    ``persona_scaffold.scaffold()``, which rescans this process's registry and
    would hide the very staleness under test.
    """
    manifest = persona_scaffold.build_app_manifest(
        app_id, name or app_id.title(), "keep a list", list(lists))
    pack = persona_scaffold.build_pack_manifest(app_id, name or app_id.title())

    app_folder = Path(app_registry.user_apps_dir()) / app_id
    app_folder.mkdir(parents=True, exist_ok=True)
    (app_folder / app_registry.MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

    pack_folder = Path(app_registry.user_packs_dir()) / app_id
    pack_folder.mkdir(parents=True, exist_ok=True)
    (pack_folder / app_registry.PACK_MANIFEST_NAME).write_text(
        json.dumps(pack, indent=2), encoding="utf-8")
    return app_folder


# ---------------------------------------------------------------------------
# 1. the repro, at both routes
# ---------------------------------------------------------------------------

class TestAnotherProcessInstall:
    def test_installed_lists_it_without_a_rescan_call(self, box):
        assert 'gardenbot' not in installed_ids(box)

        install_from_another_process('gardenbot', name="GardenBot")

        assert 'gardenbot' in installed_ids(box), (
            "the running server did not notice an app another process installed")

    def test_the_data_route_answers_without_a_rescan_call(self, box):
        """The half that used to survive even a rescan: AppData folds the
        manifests into its tables at construction and was cached for the life of
        the process, so /apps/<id>/data 404'd after /apps/installed had started
        listing the app."""
        install_from_another_process('gardenbot', name="GardenBot")

        resp = box.get('/apps/gardenbot/data', headers=AUTH)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert isinstance(resp.get_json(), dict)

    def test_a_deleted_folder_stops_being_installed(self, box):
        """The same door, the other way: uninstalling is removing a folder."""
        folder = install_from_another_process('gardenbot')
        assert 'gardenbot' in installed_ids(box)

        import shutil
        shutil.rmtree(folder)

        assert 'gardenbot' not in installed_ids(box)

    def test_the_first_ever_owner_app_is_seen(self, box, tmp_path):
        """The apps root itself does not exist yet on a fresh install, so the
        fingerprint has to treat 'the directory appeared' as a change."""
        root = Path(app_registry.user_apps_dir())
        assert not root.exists()
        installed_ids(box)                        # fingerprint the absence

        install_from_another_process('firstbot')

        assert 'firstbot' in installed_ids(box)


# ---------------------------------------------------------------------------
# 2. the module contract underneath it
# ---------------------------------------------------------------------------

class TestRegistryFingerprint:
    def test_current_manifests_does_not_rescan_when_nothing_changed(self, box):
        app_registry.current_manifests()
        generation = app_registry.generation()
        for _ in range(5):
            app_registry.current_manifests()
        assert app_registry.generation() == generation, (
            "a chat turn must not pay for a full scan")

    def test_current_manifests_rescans_exactly_once_per_change(self, box):
        app_registry.current_manifests()
        generation = app_registry.generation()

        install_from_another_process('gardenbot')
        app_registry.current_manifests()
        assert app_registry.generation() == generation + 1

        app_registry.current_manifests()
        assert app_registry.generation() == generation + 1

    def test_a_pack_root_change_counts_too(self, box):
        """load_packs resolves against these manifests, so a pack root that
        moved on its own is still a changed build."""
        app_registry.current_manifests()
        generation = app_registry.generation()

        folder = Path(app_registry.user_packs_dir()) / "loosepack"
        folder.mkdir(parents=True, exist_ok=True)

        app_registry.current_manifests()
        assert app_registry.generation() == generation + 1

    def test_an_unreadable_root_errs_towards_rescanning(self, monkeypatch, box):
        app_registry.current_manifests()
        generation = app_registry.generation()
        def _boom(root):
            raise RuntimeError("boom")

        monkeypatch.setattr(app_registry, "_root_fingerprint", _boom)
        try:
            app_registry.current_manifests()
        finally:
            monkeypatch.undo()
        assert app_registry.generation() == generation + 1


class TestAppDataFollowsTheRegistry:
    def test_the_store_cache_is_rebuilt_when_the_registry_moves(self, box):
        first = api_server._get_app_data()
        assert api_server._get_app_data() is first, "no reason to rebuild"

        install_from_another_process('gardenbot')

        assert api_server._get_app_data() is not first, (
            "AppData still holds the tables of the old manifest set")

    def test_rescan_resets_the_store_cache_too(self, box):
        first = api_server._get_app_data()
        api_server._reset_app_data()
        assert api_server._get_app_data() is not first


# ---------------------------------------------------------------------------
# 3. the CLI's best-effort nudge
# ---------------------------------------------------------------------------

class TestCliRescanNudge:
    def test_no_server_running_is_silent_and_not_an_error(self, monkeypatch):
        """The normal case for somebody scaffolding before first boot."""
        from puffpai import cli

        def _refuse(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", _refuse)
        assert cli._nudge_local_rescan() is False

    def test_it_only_ever_calls_loopback_with_the_resolved_key(self, monkeypatch):
        seen = {}

        class _Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _capture(request, timeout=None):
            seen['url'] = request.full_url
            seen['key'] = request.get_header('X-api-key')
            seen['method'] = request.get_method()
            return _Response()

        from puffpai import cli

        monkeypatch.setattr("urllib.request.urlopen", _capture)
        assert cli._nudge_local_rescan() is True
        assert seen['url'].startswith("http://127.0.0.1:"), seen['url']
        assert seen['url'].endswith("/apps/rescan")
        assert seen['method'] == "POST"
        from puffpai.security.secrets_provider import get_secret_with_source
        expected, _source = get_secret_with_source('PUFFPAI_API_KEY')
        assert seen['key'] == str(expected).strip()

    def test_no_key_means_no_call(self, monkeypatch):
        from puffpai import cli

        monkeypatch.setattr(
            "puffpai.security.secrets_provider.get_secret_with_source",
            lambda name: ("", "nowhere"))
        called = []
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **k: called.append(1))
        assert cli._nudge_local_rescan() is False
        assert not called


# ---------------------------------------------------------------------------
# 4. the explicit rescan is still the guarantee
# ---------------------------------------------------------------------------

def test_an_explicit_rescan_still_picks_up_an_in_place_manifest_edit(box):
    """The fingerprint PROMISES name-set changes (install, uninstall, scaffold);
    whether an in-place edit of an existing app.json also moves the containing
    directory's mtime is a filesystem question, so it is not asserted either way.
    ``POST /apps/rescan`` is the guarantee, and it has to keep working — that is
    what makes "edit a manifest, rescan" a complete instruction for a developer.
    """
    folder = install_from_another_process('gardenbot', name="GardenBot")
    assert 'gardenbot' in installed_ids(box)

    manifest_path = folder / app_registry.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest['name'] = "Renamed"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    resp = box.post('/apps/rescan', headers=AUTH)
    assert resp.status_code == 200
    names = {r['id']: r['name'] for r in app_registry.ui_records()}
    assert names.get('gardenbot') == "Renamed"
