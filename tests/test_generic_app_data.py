"""An app is a folder: ANY registry-installed app gets served, not just five.

The failure this guards against: ``api_server.VALID_APPS`` and
``AppData.VALID_APPS`` name the five household apps, and if ``AppData`` folded
the installed manifests in only when it is CONSTRUCTED (an instance cached for
the life of the process), dropping a folder into ``apps/`` and POSTing
``/apps/rescan`` would make ``/apps/installed`` list the new app while
``/apps/<id>/data`` answered 404 until somebody restarted the service.
"Installing is rescanning" would be true of the listing and false of the data
routes.

What is asserted here: after a rescan, a non-household app is servable at once,
its ``data.default`` IS the initial document, a PUT merges into it, the five
shipped apps are unchanged, and an id that is not in the registry is refused
before it is ever used as a path.

Deliberately NOT covered here: no chat fastpath for generic apps, and
``/apps/<id>/action`` stays the shipped apps' own action set — those are
methods on the store, and an installed app has none.

Run: pytest tests/test_generic_app_data.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import api_server as mod          # noqa: E402
from puffpai import app_data as app_data_mod   # noqa: E402
from puffpai import app_registry               # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: The example that ships in the tree, which is what an owner copies.
EXAMPLE_DIR = _REPO_ROOT / "apps" / "_examples" / "sky_log"

WORKSHOP = {
    "schema": "puffpai_app/v1",
    "id": "workshop",
    "name": "Workshop Inventory",
    "blurb": "What is in the drawers, and how much of it is left.",
    "version": "1.0.0",
    "surfaces": ["daily.games"],
    "data": {"default": {"bins": [], "parts": [], "last_updated": ""}},
    "ui": {"route": "/dashboard/apps/workshop", "icon": "mdiToolbox"},
    "requires": {"home_assistant": False, "network": False, "ollama": False},
}


@pytest.fixture(scope="module", autouse=True)
def restore_registry():
    """Rescan the REAL apps/ once this file is done.

    MODULE scope on purpose: the manifest cache is process-wide, and a
    function-scoped finalizer runs while ``monkeypatch`` still has
    ``_apps_dir`` pointed at a temp directory, so the "cleanup" would re-cache
    the temp tree and the next file would see an apps/ that no longer exists.
    That
    order-dependence is exactly what the suite's staging discipline is about;
    a module finalizer runs after the last test's patches are undone.
    """
    yield
    app_registry.rescan()
    mod._reset_app_data()


@pytest.fixture()
def installed(tmp_path, monkeypatch):
    """A temp apps/ dir holding the five shipped manifests plus one new app,
    and a temp data dir, with the registry and the store pointed at both."""
    apps_dir = tmp_path / "apps"
    apps_dir.mkdir()
    for shipped in sorted((_REPO_ROOT / "apps").iterdir()):
        if shipped.name.startswith("_") or not shipped.is_dir():
            continue
        target = apps_dir / shipped.name
        target.mkdir()
        (target / "app.json").write_text(
            (shipped / "app.json").read_text(encoding="utf-8"),
            encoding="utf-8")
    (apps_dir / "workshop").mkdir()
    (apps_dir / "workshop" / "app.json").write_text(json.dumps(WORKSHOP),
                                                    encoding="utf-8")

    data_dir = tmp_path / "app_data"
    monkeypatch.setattr(app_registry, "_apps_dir", lambda: str(apps_dir))
    monkeypatch.setattr(app_registry, "_packs_dir",
                        lambda: str(tmp_path / "packs"))
    monkeypatch.setattr(app_data_mod, "_data_dir", lambda: str(data_dir))
    app_registry.rescan()
    mod._reset_app_data()
    yield apps_dir, data_dir
    app_registry.rescan()
    mod._reset_app_data()


@pytest.fixture()
def client(installed):
    mod.app.config["TESTING"] = True
    mod._rate_limit_store.clear()
    with mod.app.test_client() as c:
        yield c, {"X-API-Key": mod._API_KEY}


# ===========================================================================
# 1. A rescan is an install, for the data routes too
# ===========================================================================

class TestRescanInstallsForRealNow:

    def test_a_new_app_is_listed_and_servable_without_a_restart(
            self, tmp_path, monkeypatch):
        apps_dir = tmp_path / "apps"
        apps_dir.mkdir()
        monkeypatch.setattr(app_registry, "_apps_dir", lambda: str(apps_dir))
        monkeypatch.setattr(app_registry, "_packs_dir",
                            lambda: str(tmp_path / "packs"))
        monkeypatch.setattr(app_data_mod, "_data_dir",
                            lambda: str(tmp_path / "app_data"))
        app_registry.rescan()
        mod._reset_app_data()
        mod.app.config["TESTING"] = True
        mod._rate_limit_store.clear()
        with mod.app.test_client() as c:
            headers = {"X-API-Key": mod._API_KEY}
            assert c.get('/apps/workshop/data',
                         headers=headers).status_code == 404

            # The whole install: a folder, and a rescan.
            (apps_dir / "workshop").mkdir()
            (apps_dir / "workshop" / "app.json").write_text(
                json.dumps(WORKSHOP), encoding="utf-8")
            rescan = c.post('/apps/rescan', headers=headers)
            assert rescan.status_code == 200
            assert 'workshop' in rescan.get_json()['apps']

            resp = c.get('/apps/workshop/data', headers=headers)
            assert resp.status_code == 200, resp.get_data(as_text=True)
            assert resp.get_json() == {"bins": [], "parts": [],
                                       "last_updated": ""}
        app_registry.rescan()
        mod._reset_app_data()

    def test_installed_lists_the_new_app(self, client):
        c, headers = client
        body = c.get('/apps/installed', headers=headers).get_json()
        ids = [row['id'] for row in body['apps']]
        assert 'workshop' in ids
        assert body['problems'] == []


# ===========================================================================
# 2. The declared shape is the initial document
# ===========================================================================

class TestGenericAppData:

    def test_the_manifest_default_is_the_first_document(self, client,
                                                        installed):
        c, headers = client
        _apps_dir, data_dir = installed
        resp = c.get('/apps/workshop/data', headers=headers)
        assert resp.status_code == 200
        assert resp.get_json() == {"bins": [], "parts": [], "last_updated": ""}
        # And it was written where the owner's files live, not held in memory.
        on_disk = json.loads((data_dir / "workshop.json").read_text(
            encoding="utf-8"))
        assert on_disk == WORKSHOP["data"]["default"]

    def test_put_merges_and_survives_a_reread(self, client):
        c, headers = client
        resp = c.put('/apps/workshop/data', headers=headers,
                     json={"parts": [{"name": "M3 bolt", "count": 40}]})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = c.get('/apps/workshop/data', headers=headers).get_json()
        assert body["parts"] == [{"name": "M3 bolt", "count": 40}]
        assert body["bins"] == []            # a key the payload omitted
        assert body["last_updated"]

    def test_updated_poll_works_for_a_generic_app(self, client):
        c, headers = client
        c.put('/apps/workshop/data', headers=headers, json={"bins": ["A1"]})
        body = c.get('/apps/workshop/updated', headers=headers).get_json()
        assert body['last_updated']

    def test_an_app_with_no_declared_default_still_serves(self, tmp_path,
                                                          monkeypatch):
        apps_dir = tmp_path / "apps"
        (apps_dir / "bare").mkdir(parents=True)
        manifest = dict(WORKSHOP, id="bare", name="Bare")
        manifest.pop("data")
        (apps_dir / "bare" / "app.json").write_text(json.dumps(manifest),
                                                    encoding="utf-8")
        monkeypatch.setattr(app_registry, "_apps_dir", lambda: str(apps_dir))
        monkeypatch.setattr(app_data_mod, "_data_dir",
                            lambda: str(tmp_path / "app_data"))
        app_registry.rescan()
        store = app_data_mod.AppData()
        assert store.get_data("bare") == {}
        app_registry.rescan()


# ===========================================================================
# 3. The five shipped apps are unchanged
# ===========================================================================

class TestShippedAppsUnchanged:

    @pytest.mark.parametrize('app_id', ['alarmflow', 'calendar', 'freshtable',
                                        'fitfam', 'trivia'])
    def test_shipped_app_keeps_its_hand_written_shape(self, client, app_id):
        c, headers = client
        body = c.get(f'/apps/{app_id}/data', headers=headers).get_json()
        assert isinstance(body, dict)
        assert 'last_updated' in body

    def test_calendar_still_ships_its_family_rows(self, client):
        c, headers = client
        body = c.get('/apps/calendar/data', headers=headers).get_json()
        assert [p['id'] for p in body['family']] == ['dad', 'mom', 'alex',
                                                     'emma']

    def test_a_shipped_action_still_runs(self, client):
        c, headers = client
        resp = c.post('/apps/alarmflow/action', headers=headers,
                      json={'action': 'alarm_create_timer',
                            'params': {'desc': 'pizza', 'minutes': 10}})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()['success'] is True


# ===========================================================================
# 4. The id is checked before it is used
# ===========================================================================

class TestIdValidation:

    @pytest.mark.parametrize('bad', [
        '../../etc/passwd', '..', 'Workshop', 'work shop', 'a',
        'x' * 41, 'workshop.json', 'work/shop', 'WORKSHOP',
    ])
    def test_a_non_registry_id_is_refused_by_every_data_route(self, client,
                                                             bad):
        c, headers = client
        for method, path in (('get', f'/apps/{bad}/data'),
                             ('get', f'/apps/{bad}/updated')):
            resp = getattr(c, method)(path, headers=headers)
            assert resp.status_code in (404, 400, 405), (path,
                                                         resp.status_code)
        resp = c.put(f'/apps/{bad}/data', headers=headers, json={"x": 1})
        assert resp.status_code in (404, 400, 405), resp.status_code

    def test_valid_app_shape_checks_before_it_reads_the_store(self,
                                                              monkeypatch):
        """The guard is the registry's id rule, not a dict lookup that happened
        to miss: it holds even when the store cannot be built."""
        def _boom():
            raise RuntimeError("store unavailable")
        monkeypatch.setattr(mod, "_get_app_data", _boom)
        assert mod._valid_app('../../etc/passwd') is False
        assert mod._valid_app('calendar') is True      # the fallback list

    def test_an_uninstalled_but_well_formed_id_is_a_404(self, client):
        c, headers = client
        assert c.get('/apps/sky_log/data',
                     headers=headers).status_code == 404


# ===========================================================================
# 5. The shipped examples
# ===========================================================================

class TestShippedExamples:

    def test_underscore_folders_are_not_installed(self):
        assert 'sky_log' not in app_registry.installed_ids(
            app_registry.load_manifests(str(_REPO_ROOT / "apps")))
        packs = app_registry.load_packs(str(_REPO_ROOT / "packs"))
        assert 'stargazing' not in [p['id'] for p in packs]

    def test_the_example_app_is_a_valid_manifest(self):
        manifests = app_registry.load_manifests(
            str(_REPO_ROOT / "apps" / "_examples"))
        assert [m['id'] for m in manifests] == ['sky_log']
        assert app_registry.default_data(manifests[0])['sessions'] == []

    def test_the_example_pack_loads_against_the_example_app(self):
        manifests = app_registry.load_manifests(
            str(_REPO_ROOT / "apps" / "_examples"))
        packs = app_registry.load_packs(str(_REPO_ROOT / "packs" / "_examples"),
                                        manifests=manifests)
        assert [p['id'] for p in packs] == ['stargazing']

    def test_copying_the_example_into_apps_installs_it(self, tmp_path,
                                                       monkeypatch):
        """The README's instructions, executed."""
        apps_dir = tmp_path / "apps"
        target = apps_dir / "sky_log"
        target.mkdir(parents=True)
        (target / "app.json").write_text(
            (EXAMPLE_DIR / "app.json").read_text(encoding="utf-8"),
            encoding="utf-8")
        monkeypatch.setattr(app_registry, "_apps_dir", lambda: str(apps_dir))
        monkeypatch.setattr(app_data_mod, "_data_dir",
                            lambda: str(tmp_path / "app_data"))
        app_registry.rescan()
        store = app_data_mod.AppData()
        assert 'sky_log' in store.VALID_APPS
        assert store.get_data('sky_log')['sessions'] == []
        app_registry.rescan()
