"""``/system/status`` publishes the app-data privacy posture — counts only.

The whole point of the block is that an owner can be told "app data holding P4
classes: 3" without the status endpoint becoming the disclosure channel the rest
of this work exists to close. ``/system/status`` is reachable by every keyed
caller on the LAN, so the strongest test here is the negative one: no household
value, and no filename, may appear in the response.

Fixtures use obviously fake values only.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import puffpai.api_server as api  # noqa: E402


CALENDAR = {
    "family": [{"name": "Test Fakechild", "role": "child"}],
    "meds": [{"name": "test-fakemed", "time": "20:00"}],
    "bills": [{"payee": "test-fakepayee", "amount": 42.0}],
    "events": [],
}
FITFAM = {"users": {"c": {"name": "Test Fakechild", "age": 15,
                          "history": [{"type": "test-fakeworkout"}]}}}
TRIVIA = {"players": [{"name": "Test Fakechild", "age": 9}],
          "results": [{"score": 3}]}

_DOCS = {"calendar": CALENDAR, "fitfam": FITFAM, "trivia": TRIVIA,
         "freshtable": {"pantry": ["test-fakeitem"]}}


class _FakeAppData:
    def get_data(self, name):
        return _DOCS.get(name, {})


@pytest.fixture
def status(monkeypatch):
    monkeypatch.setattr(api, "_get_app_data", lambda: _FakeAppData())
    with api._app_data_status_lock:
        api._app_data_status_cache["value"] = None
        api._app_data_status_cache["ts"] = 0.0
    return api._app_data_privacy_status()


def test_status_reports_the_p4_class_count(status):
    assert status["status"] == "ok"
    assert status["p4_class_count"] == 4
    assert set(status["p4_classes"]) == {
        "child.any", "financial.account_balance",
        "health.condition", "health.device_reading"}
    assert set(status["apps_tagged"]) == {"calendar", "fitfam", "trivia"}


def test_status_never_publishes_a_household_value(status):
    blob = json.dumps(status)
    for value in ("Test Fakechild", "test-fakemed", "test-fakepayee",
                  "test-fakeworkout", "test-fakeitem", "42"):
        assert value not in blob, value


def test_status_never_publishes_a_filename(status):
    blob = json.dumps(status)
    assert ".json" not in blob


def test_status_states_plainly_that_modes_are_not_encryption(status):
    """An app page must not be able to read this block as "vaulted"."""
    assert status["at_rest"]["encrypted"] is False
    assert "not built yet" in status["at_rest"]["note"]


def test_status_is_cached_then_invalidated_by_a_write(monkeypatch):
    calls = {"n": 0}

    class Counting(_FakeAppData):
        def get_data(self, name):
            calls["n"] += 1
            return _DOCS.get(name, {})

    monkeypatch.setattr(api, "_get_app_data", lambda: Counting())
    with api._app_data_status_lock:
        api._app_data_status_cache["value"] = None
        api._app_data_status_cache["ts"] = 0.0

    api._app_data_privacy_status()
    first = calls["n"]
    api._app_data_privacy_status()
    assert calls["n"] == first, "second call within the TTL must be cached"

    # A write drops the cache so the owner sees the count they just created.
    api._tag_app_write("calendar", CALENDAR)
    api._app_data_privacy_status()
    assert calls["n"] > first


@pytest.fixture
def fresh_tag_log():
    """Forget what has already been logged for each app.

    The audit line is deliberately NOT emitted on every write (see below), so
    a test that wants to see one has to start from a clean slate.
    """
    with api._app_data_status_lock:
        api._app_data_seen_classes.clear()
    yield
    with api._app_data_status_lock:
        api._app_data_seen_classes.clear()


def test_write_path_tagging_logs_classes_and_counts_only(caplog, fresh_tag_log):
    """The audit line is the other place a value could escape."""
    caplog.set_level("INFO", logger="puffpai.api_server")
    api._tag_app_write("calendar", CALENDAR)
    text = caplog.text
    assert "P4 class(es)" in text
    assert "calendar.meds[]=health.condition[1]" in text
    for value in ("test-fakemed", "test-fakepayee", "Test Fakechild"):
        assert value not in text, value


def test_repeat_writes_of_the_same_shape_do_not_flood_the_log(caplog,
                                                              fresh_tag_log):
    """The apps PUT the WHOLE document on every edit.

    An unconditional WARNING per write would put a line in journald for every
    debounced save, and the one line that matters — a new critical-PII class
    appearing — would be buried in its own noise. WARNING is reserved for a
    CHANGED class set.
    """
    caplog.set_level("INFO", logger="puffpai.api_server")
    for _ in range(5):
        api._tag_app_write("calendar", CALENDAR)
    assert caplog.text.count("app-data write to 'calendar'") == 1


def test_a_newly_appearing_p4_class_warns_even_after_earlier_writes(
        caplog, fresh_tag_log):
    """The event actually worth waking someone for."""
    caplog.set_level("INFO", logger="puffpai.api_server")
    without_meds = {k: v for k, v in CALENDAR.items() if k != "meds"}
    api._tag_app_write("calendar", without_meds)
    caplog.clear()
    api._tag_app_write("calendar", CALENDAR)   # meds[] appears
    assert "health.condition" in caplog.text
    assert "WARNING" in caplog.text


def test_write_path_tagging_is_silent_for_an_untagged_app(caplog,
                                                          fresh_tag_log):
    caplog.set_level("INFO", logger="puffpai.api_server")
    api._tag_app_write("alarmflow", {"items": [{"desc": "test-fakealarm"}]})
    assert "app-data write" not in caplog.text


def test_write_path_tagging_never_raises():
    for bad in (None, [], "string", {"meds": "not-a-list"}):
        api._tag_app_write("calendar", bad)
    api._tag_app_write("not-an-app", {"meds": [{"name": "x"}]})


def test_status_survives_an_app_that_cannot_be_read(monkeypatch):
    class Broken:
        def get_data(self, name):
            if name == "calendar":
                raise OSError("simulated read failure")
            return _DOCS.get(name, {})

    monkeypatch.setattr(api, "_get_app_data", lambda: Broken())
    with api._app_data_status_lock:
        api._app_data_status_cache["value"] = None
        api._app_data_status_cache["ts"] = 0.0

    result = api._app_data_privacy_status()
    assert result["status"] == "ok"
    assert "calendar" not in result["apps_tagged"]
    assert "child.any" in result["p4_classes"]  # fitfam and trivia still count


# ── The zero-tag write ────────────────────────────────────────────────────
#
# The cache invalidation is unconditional, so /system/status stays exact after
# any write. If `_tag_app_write` returned early on `if not tags`, a write that
# removed the LAST medication would
#
# * leave `/system/status` claiming a P4 class for up to the 30 s TTL, and
# * leave the seen-class set stale, so re-adding the medication would produce
# NO WARNING, suppressing precisely the event the WARNING exists for.
#
# Both updates are unconditional. The LOG stays conditional (an app that has never
# held a tagged class is not an audit subject), which is a different question
# from whether the cached counts are correct.

#: A calendar with nothing taggable left in it — no meds, no bills, no family
#: roles. Dropping only ``meds`` is NOT a zero-tag write (bills and the child
#: role still tag), which is the distinction these tests turn on.
EMPTY_CALENDAR = {"family": [], "meds": [], "bills": [], "events": []}


def _seed_cache(sentinel):
    with api._app_data_status_lock:
        api._app_data_status_cache["value"] = sentinel
        api._app_data_status_cache["ts"] = 1.0


def test_a_zero_tag_write_still_drops_the_status_cache(fresh_tag_log):
    """The stale-counts half of the zero-tag write."""
    _seed_cache({"p4_class_count": 1})
    api._tag_app_write("alarmflow", {"items": [{"desc": "test-fakealarm"}]})
    assert api._app_data_status_cache["value"] is None
    assert api._app_data_status_cache["ts"] == 0.0


def test_removing_the_last_medication_drops_the_status_cache(fresh_tag_log):
    """The concrete household case: the owner deletes their last medication."""
    api._tag_app_write("calendar", CALENDAR)
    _seed_cache({"p4_class_count": 4})
    api._tag_app_write("calendar", EMPTY_CALENDAR)
    assert api._app_data_status_cache["value"] is None


def test_re_adding_a_class_after_a_zero_tag_write_warns_again(caplog,
                                                              fresh_tag_log):
    """The suppressed-WARNING half of the zero-tag write.

    When the calendar's classes go to none, an early return would leave the
    seen set holding the OLD classes, and putting the medication back would
    look unchanged. A medication reappearing in the household's calendar is
    exactly the event this WARNING is reserved for.
    """
    caplog.set_level("INFO", logger="puffpai.api_server")
    api._tag_app_write("calendar", CALENDAR)
    api._tag_app_write("calendar", EMPTY_CALENDAR)
    caplog.clear()
    api._tag_app_write("calendar", CALENDAR)
    assert "WARNING" in caplog.text
    assert "health.condition" in caplog.text


def test_losing_every_class_is_itself_logged(caplog, fresh_tag_log):
    """Going from classes to none is a real change in the status counts."""
    caplog.set_level("INFO", logger="puffpai.api_server")
    api._tag_app_write("calendar", CALENDAR)
    caplog.clear()
    api._tag_app_write("calendar", EMPTY_CALENDAR)
    assert "app-data write to 'calendar'" in caplog.text
    assert "no tagged classes" in caplog.text


def test_an_app_that_never_held_a_class_stays_out_of_the_audit_log(
        caplog, fresh_tag_log):
    """while the cache still drops (asserted above). Two separate concerns:
    an AlarmFlow timer save is not an audit subject, but it can still change
    what /system/status should say about the other apps' documents."""
    caplog.set_level("INFO", logger="puffpai.api_server")
    for _ in range(3):
        api._tag_app_write("alarmflow", {"items": [{"desc": "test-fakealarm"}]})
    assert "app-data write" not in caplog.text
