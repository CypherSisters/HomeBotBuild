"""Every manifest action is a real, reachable backend method.

An action id in an `apps/*/app.json` file that is only a label (`add_event`,
`plan_week`, `start_round`,...) is not in `_ALLOWED_APP_ACTIONS`, so it 403s,
and without a backend method it is a promise printed on a wall that nothing
can keep. This file is the check in both directions, so a manifest can never
advertise a capability the box does not have.

BuildBot's manifests follow the same rule.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

APPS_DIR = ROOT / "apps"
MANIFESTS = sorted(APPS_DIR.glob("*/app.json"))


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_five_manifests_are_all_here():
    assert {p.parent.name for p in MANIFESTS} == {
        "alarmflow", "calendar", "fitfam", "freshtable", "trivia"}


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_every_action_id_is_allowlisted_for_its_own_app(path):
    from puffpai import api_server
    manifest = _load(path)
    app_id = manifest["id"]
    allowed = api_server._APP_ACTIONS.get(app_id, frozenset())
    for action in manifest.get("actions", []):
        assert action["id"] in allowed, \
            f"{app_id}: {action['id']} would 403 on /apps/{app_id}/action"
        assert action["id"] in api_server._ALLOWED_APP_ACTIONS


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_every_action_id_is_a_real_appdata_method(path):
    from puffpai.app_data import AppData
    manifest = _load(path)
    for action in manifest.get("actions", []):
        method = getattr(AppData, action["id"], None)
        assert callable(method), \
            f"{manifest['id']}: {action['id']} has no backend method"


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_every_action_has_a_says_a_person_could_actually_say(path):
    manifest = _load(path)
    actions = manifest.get("actions", [])
    assert actions, f"{manifest['id']} advertises nothing"
    seen = set()
    for action in actions:
        says = action.get("says", "")
        assert isinstance(says, str) and len(says.split()) >= 3, \
            f"{manifest['id']}: {action.get('id')} has no usable `says`"
        assert action["id"] not in seen, f"duplicate id {action['id']}"
        seen.add(action["id"])


def test_the_ids_with_no_backend_method_are_gone():
    """`plan_week` and `start_round` have no method anywhere, so no manifest
    declares them. `list_day` and `show_scores` map to real methods."""
    ids = {a["id"] for p in MANIFESTS for a in _load(p).get("actions", [])}
    assert not ids & {"plan_week", "start_round"}
    assert "cal_list_day" in ids, "list_day maps to cal_list_day"
    assert "trivia_get_stats" in ids, "show_scores maps to a real read"
    # And no bare label stands in for a method id anywhere.
    assert not ids & {"add_event", "list_day", "add_task", "create_timer",
                      "create_alarm", "list_active", "dismiss", "add_item",
                      "list_pantry", "log_workout", "show_streak",
                      "show_scores"}


def test_no_manifest_declares_an_age_in_any_data_class():
    """Trivia's data class carries no age, and no manifest may declare one."""
    for path in MANIFESTS:
        document = json.dumps(_load(path).get("data", {}))
        assert '"age"' not in document, path.parent.name
