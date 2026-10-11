"""A browser push never deletes data, over HTTP: the route the browser calls.

``PUT /apps/<name>/data`` is where a stale browser push could wipe a whole
collection (the recipes, say), so the merge, the 409 refusal and the two
opt-ins are pinned at the route rather than only at the data layer.
"""

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
def client(api_app, tmp_path, monkeypatch):
    app, api_key, mod = api_app
    mod._rate_limit_store.clear()
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    from puffpai import app_data as app_data_mod
    monkeypatch.setattr(app_data_mod, "_data_dir_cache", None)
    monkeypatch.setattr(mod, "_app_data_instance", app_data_mod.AppData(),
                        raising=False)
    with app.test_client() as c:
        yield c, {"X-API-Key": api_key}, mod


_HOUSEHOLD = {
    "pantry": [{"id": "p1", "name": "ground beef"}],
    "shoppingList": [{"id": f"s{n}"} for n in range(8)],
    "mealPlan": [{"id": f"m{n}"} for n in range(7)],
    "recipes": [{"id": f"r{n}"} for n in range(8)],
}

#: What a browser that has just seeded itself would send through syncPush.
_SEEDED_PUSH = {"pantry": [{"id": f"d{n}"} for n in range(10)],
                "shoppingList": [], "mealPlan": []}


def _seed(client, headers):
    assert client.put('/apps/freshtable/data?replace=1', json=_HOUSEHOLD,
                      headers=headers).status_code == 200


class TestThePutRoute:
    def test_a_partial_push_preserves_the_keys_it_omits(self, client):
        c, headers, _ = client
        _seed(c, headers)
        r = c.put('/apps/freshtable/data',
                  json={"pantry": [{"id": "p1"}, {"id": "p2"}]}, headers=headers)
        assert r.status_code == 200
        doc = c.get('/apps/freshtable/data', headers=headers).get_json()
        assert len(doc["recipes"]) == 8
        assert len(doc["pantry"]) == 2

    def test_a_seeded_browser_push_is_refused_with_the_counts(self, client):
        c, headers, _ = client
        _seed(c, headers)
        r = c.put('/apps/freshtable/data', json=_SEEDED_PUSH, headers=headers)
        assert r.status_code == 409
        body = r.get_json()
        assert body["error"] == "would_drop_data"
        assert body["drops"]["shoppingList"] == {"before": 8, "after": 0}
        doc = c.get('/apps/freshtable/data', headers=headers).get_json()
        assert len(doc["recipes"]) == 8
        assert len(doc["shoppingList"]) == 8

    def test_allow_shrink_keeps_merge_semantics(self, client):
        c, headers, _ = client
        _seed(c, headers)
        r = c.put('/apps/freshtable/data?allow_shrink=1', json=_SEEDED_PUSH,
                  headers=headers)
        assert r.status_code == 200
        doc = c.get('/apps/freshtable/data', headers=headers).get_json()
        assert len(doc["recipes"]) == 8          # untouched: never sent
        assert doc["shoppingList"] == []          # emptied: meant

    def test_replace_is_a_true_replace(self, client):
        c, headers, _ = client
        _seed(c, headers)
        r = c.put('/apps/freshtable/data?replace=1', json=_SEEDED_PUSH,
                  headers=headers)
        assert r.status_code == 200
        doc = c.get('/apps/freshtable/data', headers=headers).get_json()
        assert "recipes" not in doc

    def test_the_version_climbs_across_route_writes(self, client):
        c, headers, _ = client
        _seed(c, headers)
        c.put('/apps/freshtable/data', json={"pantry": [{"id": "p1"}]},
              headers=headers)
        first = c.get('/apps/freshtable/data', headers=headers).get_json()
        c.put('/apps/freshtable/data', json={"pantry": [{"id": "p1"}]},
              headers=headers)
        second = c.get('/apps/freshtable/data', headers=headers).get_json()
        assert second["data_version"] == first["data_version"] + 1

    def test_the_previous_document_is_recoverable_from_disk(self, client):
        c, headers, mod = client
        _seed(c, headers)
        c.put('/apps/freshtable/data?allow_shrink=1', json=_SEEDED_PUSH,
              headers=headers)
        import json as _json
        ad = mod._get_app_data()
        backup = os.path.join(os.path.dirname(ad.data_path("freshtable")),
                              "backups", "freshtable.1.json")
        assert os.path.exists(backup)
        with open(backup, encoding="utf-8") as handle:
            assert len(_json.load(handle)["shoppingList"]) == 8

    def test_an_unknown_app_is_still_a_404(self, client):
        c, headers, _ = client
        assert c.put('/apps/nosuchapp/data', json={}, headers=headers).status_code == 404
