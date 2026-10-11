"""a browser push must never delete what it does not carry.

The live repro: FreshTable's page sends ``{pantry, shoppingList, mealPlan}``
and the canonical store also holds ``recipes``. ``PUT /apps/freshtable/data``
replaced the document wholesale, so opening the app in a second browser — where
it seeded a demo pantry and auto-synced 800 ms later — deleted the household's
entire saved recipe collection.

Three layers are pinned here: the merge, the drop guard, and the backups.
Fixture values are invented; no household contents.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import app_data as app_data_mod  # noqa: E402
from puffpai import app_data_merge  # noqa: E402

_ENV = "PUFFPAI_DATA_DIR"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(app_data_mod, "_data_dir_cache", None)
    return app_data_mod.AppData()


def _household():
    """The shape the live document has: four lists, one of them recipes."""
    return {
        "pantry": [{"id": "p1", "name": "ground beef"}],
        "shoppingList": [{"id": f"s{n}", "name": f"item {n}"} for n in range(8)],
        "mealPlan": [{"id": f"m{n}", "day": d, "meal": "dinner", "dish": f"dish {n}"}
                     for n, d in enumerate(("monday", "tuesday", "wednesday",
                                            "thursday", "friday", "saturday",
                                            "sunday"))],
        "recipes": [{"id": f"r{n}", "name": f"recipe {n}"} for n in range(8)],
    }


def _seeded_browser_push():
    """EXACTLY what syncPush used to send from a fresh browser: the three
    keys it knows, the pantry full of the demo seed, no ``recipes``."""
    return {
        "pantry": [{"id": f"d{n}", "name": f"demo {n}"} for n in range(10)],
        "shoppingList": [],
        "mealPlan": [],
    }


class TestTheFT10Sequence:
    def test_a_fresh_client_push_leaves_recipes_intact(self, app):
        app.set_data("freshtable", _household())

        # The demo seed empties two lists, which the guard refuses. That is the
        # first line of defence and it is what a real fresh browser now hits.
        with pytest.raises(app_data_merge.AppDataWouldDropData):
            app.set_data("freshtable", _seeded_browser_push(), merge=True)

        # Even when the client means it, recipes — a key it never carries —
        # survive, because absence is not deletion.
        app.set_data("freshtable", _seeded_browser_push(), merge=True,
                     allow_shrink=True)
        after = app.get_data("freshtable")
        assert len(after["recipes"]) == 8
        assert len(after["pantry"]) == 10

    def test_the_old_replace_still_destroys_it(self, app):
        """The behaviour the row reported, kept reachable only on purpose."""
        app.set_data("freshtable", _household())
        app.set_data("freshtable", _seeded_browser_push())  # merge=False
        assert "recipes" not in app.get_data("freshtable")


class TestMerge:
    def test_absent_keys_are_preserved(self, app):
        app.set_data("freshtable", _household())
        app.set_data("freshtable", {"pantry": [{"id": "p1"}, {"id": "p2"}]},
                     merge=True)
        after = app.get_data("freshtable")
        assert len(after["recipes"]) == 8
        assert len(after["shoppingList"]) == 8
        assert len(after["mealPlan"]) == 7
        assert len(after["pantry"]) == 2

    def test_explicit_null_deletes_a_key(self, app):
        app.set_data("freshtable", _household())
        app.set_data("freshtable", {"recipes": None}, merge=True,
                     allow_shrink=True)
        assert "recipes" not in app.get_data("freshtable")

    def test_a_null_still_meets_the_guard(self, app):
        app.set_data("freshtable", _household())
        with pytest.raises(app_data_merge.AppDataWouldDropData) as caught:
            app.set_data("freshtable", {"recipes": None}, merge=True)
        assert caught.value.drops["recipes"] == {"before": 8, "after": 0}

    def test_new_keys_are_added(self, app):
        app.set_data("freshtable", _household())
        app.set_data("freshtable", {"favourites": ["a"]}, merge=True)
        assert app.get_data("freshtable")["favourites"] == ["a"]


class TestDropGuard:
    def test_it_refuses_a_total_drop_with_the_counts(self, app):
        app.set_data("freshtable", _household())
        with pytest.raises(app_data_merge.AppDataWouldDropData) as caught:
            app.set_data("freshtable", {"shoppingList": []}, merge=True)
        assert caught.value.drops["shoppingList"] == {"before": 8, "after": 0}
        assert len(app.get_data("freshtable")["shoppingList"]) == 8

    def test_more_than_half_is_refused(self, app):
        app.set_data("freshtable", _household())
        with pytest.raises(app_data_merge.AppDataWouldDropData):
            app.set_data("freshtable",
                         {"shoppingList": [{"id": "s1"}, {"id": "s2"},
                                           {"id": "s3"}]}, merge=True)

    def test_exactly_half_is_allowed(self, app):
        """Crossing off half a shopping list is normal family use."""
        app.set_data("freshtable", _household())
        app.set_data("freshtable",
                     {"shoppingList": [{"id": f"s{n}"} for n in range(4)]},
                     merge=True)
        assert len(app.get_data("freshtable")["shoppingList"]) == 4

    def test_growth_is_never_a_drop(self, app):
        app.set_data("freshtable", _household())
        app.set_data("freshtable",
                     {"shoppingList": [{"id": f"s{n}"} for n in range(20)]},
                     merge=True)
        assert len(app.get_data("freshtable")["shoppingList"]) == 20

    def test_an_empty_list_can_always_be_filled_or_left(self, app):
        app.set_data("freshtable", {"pantry": [], "recipes": []})
        app.set_data("freshtable", {"pantry": []}, merge=True)
        assert app.get_data("freshtable")["pantry"] == []

    def test_allow_shrink_lets_a_meant_deletion_through(self, app):
        app.set_data("freshtable", _household())
        app.set_data("freshtable", {"shoppingList": []}, merge=True,
                     allow_shrink=True)
        assert app.get_data("freshtable")["shoppingList"] == []
        assert len(app.get_data("freshtable")["recipes"]) == 8


class TestVersioning:
    def test_each_merged_write_bumps_the_version(self, app):
        app.set_data("freshtable", _household())
        for expected in (1, 2, 3):
            app.set_data("freshtable", {"pantry": [{"id": "p1"}]}, merge=True)
            assert app.get_data("freshtable")["data_version"] == expected

    def test_a_junk_version_restarts_at_one(self, app):
        app.set_data("freshtable", dict(_household(), data_version="nonsense"))
        app.set_data("freshtable", {"pantry": [{"id": "p1"}]}, merge=True)
        assert app.get_data("freshtable")["data_version"] == 1


class TestBackups:
    def _backups(self, app):
        directory = os.path.join(
            os.path.dirname(app.data_path("freshtable")), "backups")
        if not os.path.isdir(directory):
            return []
        return sorted(os.listdir(directory))

    def test_the_previous_document_is_kept(self, app):
        app.set_data("freshtable", _household())
        app.set_data("freshtable", {"pantry": [{"id": "p1"}]}, merge=True,
                     backup=True)
        assert self._backups(app) == ["freshtable.1.json"]
        path = os.path.join(os.path.dirname(app.data_path("freshtable")),
                            "backups", "freshtable.1.json")
        with open(path, encoding="utf-8") as handle:
            assert len(json.load(handle)["recipes"]) == 8

    def test_it_rotates_five_generations_and_no_more(self, app):
        app.set_data("freshtable", _household())
        for n in range(8):
            app.set_data("freshtable", {"pantry": [{"id": f"p{n}"}]},
                         merge=True, backup=True)
        assert self._backups(app) == [f"freshtable.{n}.json" for n in range(1, 6)]

    def test_generation_one_is_the_most_recent(self, app):
        app.set_data("freshtable", _household())
        app.set_data("freshtable", {"pantry": [{"id": "first"}]}, merge=True,
                     backup=True)
        app.set_data("freshtable", {"pantry": [{"id": "second"}]}, merge=True,
                     backup=True)
        directory = os.path.join(
            os.path.dirname(app.data_path("freshtable")), "backups")
        with open(os.path.join(directory, "freshtable.1.json"),
                  encoding="utf-8") as handle:
            assert json.load(handle)["pantry"] == [{"id": "first"}]
        with open(os.path.join(directory, "freshtable.2.json"),
                  encoding="utf-8") as handle:
            assert len(json.load(handle)["pantry"]) == 1

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits only")
    def test_backups_are_owner_only(self, app):
        app.set_data("freshtable", _household())
        app.set_data("freshtable", {"pantry": [{"id": "p1"}]}, merge=True,
                     backup=True)
        path = os.path.join(os.path.dirname(app.data_path("freshtable")),
                            "backups", "freshtable.1.json")
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"

    def test_a_failing_backup_never_costs_the_write(self, app, monkeypatch):
        app.set_data("freshtable", _household())
        monkeypatch.setattr(app_data_merge, "backup_dir",
                            lambda path: (_ for _ in ()).throw(OSError("full")))
        app.set_data("freshtable", {"pantry": [{"id": "p9"}]}, merge=True,
                     backup=True)
        assert app.get_data("freshtable")["pantry"] == [{"id": "p9"}]
