"""Schema-position PII tagging for app documents.

Every fixture below uses OBVIOUSLY FAKE values ("Test Fakeperson",
"test-fakemed"). The tagger never reads a value, so the values exist only to
prove that: the assertions are all about classes, tiers, paths and counts, and
one test asserts directly that no fixture value can appear in the output.

FitFam holds kids' profiles and a Trivia document can carry player names and
ages, so both are tagged here alongside the calendar.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from puffpai.security import app_data_tags as tags  # noqa: E402
from puffpai.security.data_classifier import DataClassifier  # noqa: E402


def _classes(result):
    return {t["class"] for t in result}


def _by_path(result):
    return {t["path"]: t for t in result}


# ── Calendar ────────────────────────────────────────────────────────────────

CALENDAR_DOC = {
    "family": [
        {"name": "Test Fakeperson", "role": "parent"},
        {"name": "Test Fakechild", "role": "child"},
    ],
    "events": [{"title": "test-fakeevent", "date": "2026-09-06"}],
    "meds": [
        {"name": "test-fakemed", "time": "20:00"},
        {"name": "test-fakemed-2", "time": "08:00"},
    ],
    "bills": [{"payee": "test-fakepayee", "amount": 42.00}],
}


def test_calendar_meds_are_health_condition_p4():
    result = tags.tag_document("calendar", CALENDAR_DOC)
    med = _by_path(result)["calendar.meds[]"]
    assert med["class"] == tags.CLS_HEALTH_CONDITION
    assert med["tier"] == tags.TIER_P4
    assert med["severity"] == "critical"
    assert med["count"] == 2


def test_calendar_bills_are_financial_p4():
    bill = _by_path(tags.tag_document("calendar", CALENDAR_DOC))["calendar.bills[]"]
    assert bill["class"] == tags.CLS_FINANCIAL
    assert bill["tier"] == tags.TIER_P4


def test_calendar_child_role_is_tagged():
    """``role: "child"`` is one of only two minor signals that exist anywhere."""
    child = _by_path(tags.tag_document("calendar", CALENDAR_DOC))[
        "calendar.family[].role"]
    assert child["class"] == tags.CLS_CHILD
    assert child["tier"] == tags.TIER_P4
    assert child["count"] == 1  # the parent must NOT be counted


def test_calendar_events_alone_are_not_tagged():
    """Only the positions the taxonomy actually classifies get a tag.

    A document with events but no meds/bills/minors produces nothing, so a tag
    is a real signal rather than a thing every document has.
    """
    assert tags.tag_document("calendar", {"events": [{"title": "x"}]}) == []


# ── FitFam (kids' profiles) ─────────────────────────────────────────────────

FITFAM_DOC = {
    "users": {
        "test-fakeadult": {"name": "Test Fakeperson", "age": 41,
                           "history": [{"type": "test-fakeworkout"}]},
        "test-fakechild": {"name": "Test Fakechild", "age": 15,
                           "history": [{"type": "test-fakeworkout"},
                                       {"type": "test-fakeworkout"}],
                           "prefs": {"type": "test-fakepref"}},
    },
    "history": [{"type": "test-fakeworkout"}],
    "prefs": {"type": "test-fakepref"},
}


def test_fitfam_minor_is_child_any_p4():
    child = _by_path(tags.tag_document("fitfam", FITFAM_DOC))["fitfam.users{}.age"]
    assert child["class"] == tags.CLS_CHILD
    assert child["tier"] == tags.TIER_P4
    assert child["count"] == 1  # the 41-year-old is not a minor


def test_fitfam_history_and_prefs_are_device_readings_p4():
    result = _by_path(tags.tag_document("fitfam", FITFAM_DOC))
    for path in ("fitfam.history[]", "fitfam.prefs",
                 "fitfam.users{}.history[]", "fitfam.users{}.prefs"):
        assert result[path]["class"] == tags.CLS_HEALTH_DEVICE_READING, path
        assert result[path]["tier"] == tags.TIER_P4, path
    assert result["fitfam.users{}.history[]"]["count"] == 3


def test_fitfam_users_as_a_list_is_tagged_the_same():
    """Either shape. A tagger that only knew the map would go quiet on a
    schema normalisation and nobody would notice."""
    listed = {"users": [{"name": "Test Fakechild", "age": 15}]}
    assert tags.CLS_CHILD in _classes(tags.tag_document("fitfam", listed))


def test_fitfam_adults_only_yields_no_child_tag():
    adults = {"users": {"a": {"name": "Test Fakeperson", "age": 41}}}
    assert tags.CLS_CHILD not in _classes(tags.tag_document("fitfam", adults))


def test_missing_age_is_not_treated_as_a_minor():
    """The documented, deliberate limit: a missing age is a GAP, not a minor.

    Rounding up here would put a P4 child flag on every ageless adult in the
    house, and a tier that fires on everything is a tier the owner stops
    reading. Privacy doc §11 states the same limit.
    """
    ageless = {"users": {"a": {"name": "Test Fakeperson"}}}
    assert tags.CLS_CHILD not in _classes(tags.tag_document("fitfam", ageless))


def test_string_age_still_counts():
    """A JSON round-trip through an HTML form yields "15", not 15."""
    doc = {"users": {"a": {"name": "Test Fakechild", "age": "15"}}}
    assert tags.CLS_CHILD in _classes(tags.tag_document("fitfam", doc))


# ── FreshTable ──────────────────────────────────────────────────────────────

def test_freshtable_dietary_prefs_are_health_condition_p4():
    doc = {"familyMembers": [
        {"name": "Test Fakeperson", "prefs": ["test-fakediet"]},
        {"name": "Test Fakechild", "prefs": ["test-fakediet", "test-fakediet2"]},
    ]}
    pref = _by_path(tags.tag_document("freshtable", doc))[
        "freshtable.familyMembers[].prefs"]
    assert pref["class"] == tags.CLS_HEALTH_CONDITION
    assert pref["tier"] == tags.TIER_P4
    assert pref["count"] == 3


# ── Trivia (player names and ages) ──────────────────────────────────────────

TRIVIA_DOC = {
    "players": [{"name": "Test Fakechild", "age": 9},
                {"name": "Test Fakeperson", "age": 41}],
    "results": [{"score": 3}, {"score": 5}],
}


def test_trivia_minor_players_are_child_any_p4():
    result = _by_path(tags.tag_document("trivia", TRIVIA_DOC))
    assert result["trivia.players[].age"]["class"] == tags.CLS_CHILD
    assert result["trivia.players[].age"]["count"] == 1
    assert result["trivia.results[]"]["class"] == tags.CLS_CHILD
    assert result["trivia.results[]"]["count"] == 2


def test_trivia_adults_only_yields_nothing():
    """A score history is a children's record because of WHO it is about.

    With no minor on the roster there is no minor signal, and asserting one
    would be a guess dressed up as a detection.
    """
    adults = {"players": [{"name": "Test Fakeperson", "age": 41}],
              "results": [{"score": 3}]}
    assert tags.tag_document("trivia", adults) == []


# ── Cross-cutting guarantees ────────────────────────────────────────────────

def test_alarmflow_and_mediahub_produce_no_tags():
    assert tags.tag_document("alarmflow", {"items": [{"desc": "x"}]}) == []
    assert tags.tag_document("mediahub", {"radio": {"status": "idle"}}) == []


def test_skipped_apps_are_never_tagged():
    """Anything named in SKIPPED_APPS is left alone by the tagger."""
    assert tags.SKIPPED_APPS == {}
    summary = tags.tag_documents({"calendar": CALENDAR_DOC})
    assert set(summary["apps_tagged"]) <= {"calendar"}


def test_output_contains_no_document_values():
    """The reviewable claim: tagging cannot become a disclosure channel."""
    docs = {"calendar": CALENDAR_DOC, "fitfam": FITFAM_DOC,
            "trivia": TRIVIA_DOC}
    blob = json.dumps([tags.tag_document(a, d) for a, d in docs.items()])
    blob += json.dumps(tags.tag_documents(docs))
    for value in ("Test Fakeperson", "Test Fakechild", "test-fakemed",
                  "test-fakepayee", "test-fakeworkout", "test-fakepref", "42"):
        assert value not in blob, value


def test_summary_counts_distinct_p4_classes():
    summary = tags.tag_documents(
        {"calendar": CALENDAR_DOC, "fitfam": FITFAM_DOC, "trivia": TRIVIA_DOC})
    assert summary["p4_classes"] == sorted([
        tags.CLS_CHILD, tags.CLS_FINANCIAL,
        tags.CLS_HEALTH_CONDITION, tags.CLS_HEALTH_DEVICE_READING])
    assert summary["p4_class_count"] == 4
    assert summary["p3_class_count"] == 0
    assert set(summary["apps_tagged"]) == {"calendar", "fitfam", "trivia"}


def test_tier_ladder_bridges_to_the_scrub_vault_severities():
    """P3/P4 and critical/high must not be two ladders that can disagree."""
    from puffpai.security.scrub_and_vault import _TYPE_TIER

    assert tags.P_TIER_SEVERITY[tags.TIER_P4] == "critical"
    assert tags.P_TIER_SEVERITY[tags.TIER_P3] == "high"
    assert _TYPE_TIER[tags.CLS_CHILD] == "critical"
    assert _TYPE_TIER[tags.CLS_HEALTH_CONDITION] == "high"


def test_garbage_documents_never_raise():
    for bad in (None, [], "string", 42, {"meds": "not-a-list"},
                {"users": {"a": "not-a-dict"}}):
        assert isinstance(tags.tag_document("calendar", bad), list)
        assert isinstance(tags.tag_document("fitfam", bad), list)


def test_unknown_app_produces_no_tags():
    assert tags.tag_document("not-an-app", {"meds": [{"name": "x"}]}) == []


# ── The classifier's structured detectors ───────────────────────────────────

def test_classifier_gains_structured_health_and_child_detectors():
    """The two new classes, sourced from schema position rather than regex."""
    c = DataClassifier()

    cal = c.scan_structured_document("calendar", CALENDAR_DOC)
    assert cal.has_pii
    assert "health.condition" in cal.pii_types      # meds[]
    assert "child.any" in cal.pii_types             # family[] role child
    assert cal.severity == "critical"

    fit = c.scan_structured_document("fitfam", FITFAM_DOC)
    assert "child.any" in fit.pii_types

    triv = c.scan_structured_document("trivia", TRIVIA_DOC)
    assert "child.any" in triv.pii_types


def test_structured_detection_carries_positions_not_values():
    c = DataClassifier()
    detection = c.scan_structured_document("calendar", CALENDAR_DOC)
    blob = json.dumps(detection.health_conditions + detection.child_indicators)
    assert "test-fakemed" not in blob
    assert "Test Fakechild" not in blob
    assert "calendar:health.condition#1" in blob


def test_structured_scan_of_an_untagged_app_is_clean():
    c = DataClassifier()
    assert not c.scan_structured_document(
        "alarmflow", {"items": [{"desc": "x"}]}).has_pii
