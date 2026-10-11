"""`GET /apps/calendar/occurrences` — the read-only week-view contract.

The dashboard's week view codes against exactly this shape:

    {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD",
     "occurrences": [{"occurrence_id", "event_id", "title", "start", "end",
                      "all_day", "person", "kind", "label_only"},...]}

`kind` is "event" | "task" | "med"; `person` is a family member id or null;
`title` is the only speakable string, so a vaulted medication reports its
LABEL with `label_only: true` and its real name appears nowhere in the
payload.

Real `AppData` on a temp data dir; the route is exercised through Flask's
test client. Every row is invented.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from puffpai import app_data as app_data_module
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_module, "_data_dir_cache",
                        str(tmp_path / "app_data"), raising=False)
    return app_data_module.AppData()


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


def _monday(today=None):
    today = today or date.today()
    return today - timedelta(days=today.weekday())


def _iso(when):
    return when.replace(microsecond=0).isoformat()


ROW_KEYS = {"occurrence_id", "event_id", "title", "start", "end", "all_day",
            "person", "kind", "label_only"}


# ── the store-side assembler ────────────────────────────────────────────────

def test_every_row_carries_exactly_the_contracted_keys(app):
    monday = _monday()
    app.cal_add_event("test-fakeevent-one",
                      _iso(datetime.combine(monday, datetime.min.time())
                           .replace(hour=9)))
    rows = app.cal_occurrences(
        datetime.combine(monday, datetime.min.time()),
        datetime.combine(monday + timedelta(days=6), datetime.max.time())
        .replace(microsecond=0))
    assert rows
    for row in rows:
        assert set(row) == ROW_KEYS
        assert row["kind"] in ("event", "task", "med")


def test_a_weekly_event_expands_to_one_occurrence_per_week_in_range(app):
    start = datetime.combine(_monday() - timedelta(days=28),
                             datetime.min.time()).replace(hour=10)
    app.cal_add_event("test-fakeevent-weekly", _iso(start),
                      recurrence="weekly")
    first = datetime.combine(_monday(), datetime.min.time())
    rows = app.cal_occurrences(first, first + timedelta(days=27, hours=23))
    weekly = [r for r in rows if r["title"] == "test-fakeevent-weekly"]
    assert len(weekly) == 4, [r["start"] for r in weekly]
    assert len({r["occurrence_id"] for r in weekly}) == 4
    assert all(r["event_id"] == weekly[0]["event_id"] for r in weekly)


def test_a_task_is_reported_as_a_task(app):
    due = (date.today() + timedelta(days=1)).isoformat()
    app.cal_add_task("test-fake call the vet", due)
    first = datetime.combine(date.today(), datetime.min.time())
    rows = app.cal_occurrences(first, first + timedelta(days=6, hours=23))
    kinds = {r["title"]: r["kind"] for r in rows}
    assert kinds.get("test-fake call the vet") == "task"


def test_a_vaulted_medication_shows_its_label_and_never_its_name(app):
    app.cal_add_med("Fakezepam", "5mg", "mom", "8:00 AM")
    app.cal_vault_med("Fakezepam", "morning pills")
    first = datetime.combine(date.today(), datetime.min.time())
    rows = app.cal_occurrences(first, first + timedelta(days=6, hours=23))
    meds = [r for r in rows if r["kind"] == "med"]
    assert meds, "the medication schedule produced nothing"
    assert any(r["title"] == "morning pills" for r in meds)
    for row in meds:
        assert row["label_only"] is True
    assert "Fakezepam" not in json.dumps(rows)


def test_an_unvaulted_medication_keeps_its_name(app):
    app.cal_add_med("test-fake vitamin", "1 tab", "all", "8:00 AM")
    first = datetime.combine(date.today(), datetime.min.time())
    rows = app.cal_occurrences(first, first + timedelta(days=6, hours=23))
    meds = [r for r in rows if r["kind"] == "med"]
    assert any(r["title"] == "test-fake vitamin" and r["label_only"] is False
               for r in meds)


def test_person_is_a_member_id_or_null(app):
    when = datetime.combine(date.today(), datetime.min.time()).replace(hour=11)
    app.cal_add_event("test-fakeevent-mine", _iso(when), assigned_to="mom")
    app.cal_add_event("test-fakeevent-ours", _iso(when), assigned_to="all")
    first = datetime.combine(date.today(), datetime.min.time())
    rows = {r["title"]: r for r in
            app.cal_occurrences(first, first + timedelta(days=1))}
    assert rows["test-fakeevent-mine"]["person"] == "mom"
    assert rows["test-fakeevent-ours"]["person"] is None


def test_rows_are_sorted_by_start(app):
    day = datetime.combine(date.today(), datetime.min.time())
    for hour in (15, 8, 11):
        app.cal_add_event(f"test-fakeevent-{hour}", _iso(day.replace(hour=hour)))
    rows = app.cal_occurrences(day, day + timedelta(days=1))
    starts = [r["start"] for r in rows]
    assert starts == sorted(starts)


def test_an_event_carries_its_own_end(app):
    start = datetime.combine(date.today(), datetime.min.time()).replace(hour=9)
    app.cal_add_event("test-fakeevent-long", _iso(start),
                      _iso(start + timedelta(hours=2)))
    rows = [r for r in app.cal_occurrences(start - timedelta(hours=1),
                                           start + timedelta(hours=6))
            if r["title"] == "test-fakeevent-long"]
    assert rows
    assert rows[0]["end"] == (start + timedelta(hours=2)).isoformat()
    assert rows[0]["all_day"] is False


def test_a_dateless_row_reads_as_all_day(app):
    today = date.today().isoformat()
    app.cal_add_event("test-fakeevent-allday", today, today)
    day = datetime.combine(date.today(), datetime.min.time())
    rows = [r for r in app.cal_occurrences(day, day + timedelta(days=1))
            if r["title"] == "test-fakeevent-allday"]
    assert rows and rows[0]["all_day"] is True


# ── the route ───────────────────────────────────────────────────────────────

def test_the_route_exists_and_is_not_public(api):
    rules = {r.rule for r in api.app.url_map.iter_rules()}
    assert "/apps/calendar/occurrences" in rules
    assert "app_calendar_occurrences" not in api._PUBLIC_ENDPOINTS


def test_a_bare_lan_caller_gets_401(api):
    with api.app.test_client() as c:
        response = c.get("/apps/calendar/occurrences",
                         environ_base={"REMOTE_ADDR": "192.168.50.99"})
    assert response.status_code == 401


def test_a_keyed_caller_gets_the_current_week_by_default(api):
    with api.app.test_client() as c:
        response = c.get("/apps/calendar/occurrences",
                         headers={"X-API-Key": api._API_KEY})
    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == {"from", "to", "occurrences"}
    monday = _monday()
    assert body["from"] == monday.isoformat()
    assert body["to"] == (monday + timedelta(days=6)).isoformat()
    assert isinstance(body["occurrences"], list)
    for row in body["occurrences"]:
        assert set(row) == ROW_KEYS


def test_a_range_wider_than_31_days_is_refused(api):
    with api.app.test_client() as c:
        response = c.get("/apps/calendar/occurrences"
                         "?from=2026-01-01&to=2026-02-05",
                         headers={"X-API-Key": api._API_KEY})
    assert response.status_code == 400
    assert "31 days" in response.get_json()["error"]


def test_exactly_31_days_is_allowed(api):
    with api.app.test_client() as c:
        response = c.get("/apps/calendar/occurrences"
                         "?from=2026-01-01&to=2026-01-31",
                         headers={"X-API-Key": api._API_KEY})
    assert response.status_code == 200
    assert response.get_json()["to"] == "2026-01-31"


def test_a_backwards_or_unparseable_range_is_refused(api):
    with api.app.test_client() as c:
        head = {"X-API-Key": api._API_KEY}
        assert c.get("/apps/calendar/occurrences"
                     "?from=2026-02-01&to=2026-01-01",
                     headers=head).status_code == 400
        assert c.get("/apps/calendar/occurrences?from=last%20tuesday",
                     headers=head).status_code == 400


def test_the_route_is_read_only(api):
    with api.app.test_client() as c:
        head = {"X-API-Key": api._API_KEY}
        for method in (c.post, c.put, c.delete):
            assert method("/apps/calendar/occurrences",
                          headers=head).status_code == 405
