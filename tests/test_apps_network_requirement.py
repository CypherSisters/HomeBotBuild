"""`requirements.network` reads the door's real key, and the app's own doors.

Found by the doors security pass: `_requirement_availability`
read `d["value"]` from `egress_doors.door_status` rows, which carry `open`
and have never carried `value`. `network` was therefore permanently `False`
and every app declaring `requires.network` was permanently degraded.

Composes with `sec-manifest-doors`,
which registers each manifest door as `app:<app_id>:<n>`: an app that declares
`requires.network` is satisfied only when ITS OWN doors are open — weather
being allowed out says nothing about whether this app can reach the host it
named. Until that registration lands in this tree no app has such a door and
the global "is anything open" answer stands, which is what the fallback test
pins.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


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


@pytest.fixture(autouse=True)
def _drop_the_probe_cache(api):
    """The probe caches for 60s. Leaving a fixture's answer in it is how one
    test file poisons the ones that run after it."""
    yield
    # CLEAR it, do not repopulate: forcing a real probe here would cache this
    # box's momentary ollama answer for 60s and poison the files that follow.
    api._req_cache["value"] = None
    api._req_cache["at"] = 0.0


def test_the_probe_reads_open_not_value(api, monkeypatch):
    from puffpai.security import egress_doors

    rows = [{"id": "weather", "open": True, "value": None},
            {"id": "web_search", "open": False}]
    monkeypatch.setattr(egress_doors, "door_status", lambda: rows)
    assert api._requirement_availability(force=True)["network"] is True

    rows[0]["open"] = False
    assert api._requirement_availability(force=True)["network"] is False


def test_no_row_at_all_is_not_an_open_door(api, monkeypatch):
    from puffpai.security import egress_doors
    monkeypatch.setattr(egress_doors, "door_status", lambda: [])
    assert api._requirement_availability(force=True)["network"] is False


def test_a_probe_that_raises_still_answers_none(api, monkeypatch):
    from puffpai.security import egress_doors

    def _boom():
        raise RuntimeError("no door table")

    monkeypatch.setattr(egress_doors, "door_status", _boom)
    assert api._requirement_availability(force=True)["network"] is None


def test_an_app_with_its_own_doors_is_answered_from_those(api, monkeypatch):
    from puffpai.security import egress_doors

    rows = [{"id": "weather", "open": True},
            {"id": "app:freshtable:0", "open": False}]
    monkeypatch.setattr(egress_doors, "door_status", lambda: rows)
    api._requirement_availability(force=True)
    assert api._requirement_availability(app_id="freshtable")["network"] is False
    # An app with no doors of its own falls back to the global answer.
    assert api._requirement_availability(app_id="calendar")["network"] is True

    rows[1]["open"] = True
    api._requirement_availability(force=True)
    assert api._requirement_availability(app_id="freshtable")["network"] is True


def test_every_door_the_app_declared_must_be_open(api, monkeypatch):
    from puffpai.security import egress_doors

    rows = [{"id": "app:freshtable:0", "open": True},
            {"id": "app:freshtable:1", "open": False}]
    monkeypatch.setattr(egress_doors, "door_status", lambda: rows)
    api._requirement_availability(force=True)
    assert api._requirement_availability(app_id="freshtable")["network"] is False


def test_one_apps_doors_never_answer_for_another(api, monkeypatch):
    from puffpai.security import egress_doors

    monkeypatch.setattr(egress_doors, "door_status",
                        lambda: [{"id": "app:freshtable:0", "open": True}])
    api._requirement_availability(force=True)
    assert api._requirement_availability(app_id="freshtable")["network"] is True
    # trivia has no door of its own, so it gets the global answer — which here
    # is True only because freshtable's door is open. Pinned so the fallback
    # is a deliberate choice rather than an accident.
    assert api._requirement_availability(app_id="trivia")["network"] is True


def test_the_per_app_view_never_mutates_the_cache(api, monkeypatch):
    from puffpai.security import egress_doors

    monkeypatch.setattr(egress_doors, "door_status",
                        lambda: [{"id": "weather", "open": True},
                                 {"id": "app:freshtable:0", "open": False}])
    globalv = api._requirement_availability(force=True)
    api._requirement_availability(app_id="freshtable")
    assert globalv["network"] is True
    assert api._requirement_availability()["network"] is True


def test_the_probe_still_answers_all_three_keys(api):
    available = api._requirement_availability(force=True)
    assert set(available) == {"ollama", "home_assistant", "network"}
    for value in available.values():
        assert value is True or value is False or value is None
