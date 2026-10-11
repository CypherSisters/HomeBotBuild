"""PuffPAI must never misstate what it kept: the medication case.

WHAT THIS GUARDS
----------------
``PUFFPAI_SCRUB_AND_VAULT`` is unset on the appliance by default, so the legacy
chat branch is the one that runs. If that branch ran the free-text
``health.condition`` detector (``_scan_and_redact_message(...,
include_health=True)``), every chat surface would do this:

    _prepare_user_message('add insulin to my meds', source='chat')
      -> persisted 'add [REDACTED-HEALTH] to my [REDACTED-HEALTH]'
      -> pii_meta {'types': ['health.condition'], 'vaulted': []}
    _apply_pii_honesty("medication 'insulin' added", meta)
      -> "I spotted a health detail and did not keep it - it was removed from
          this conversation and not saved anywhere.

          medication 'insulin' added"

while the medication IS kept: the deterministic repair path writes it to
``calendar.meds[]``, which is tagged ``health.condition``/P4. And
``"I took my medicine"`` would have its whole reply *replaced* by that notice.

That is the point: **the calendar keeps a medication list**, and
hiding one is a per-med opt-in. So "I did not keep it" on a medication turn is
not a stricter privacy posture, it is a false statement about the household's
own data, and worse, because a person who believes the dose did not log will
log it twice or not at all.

Each of the four phrasings below asserts two things together, which is the
only assertion that means anything here: **what the reply says, and what the
file on disk actually holds.**

Fake data only: no real household names anywhere.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from puffpai import app_data as app_data_mod  # noqa: E402

_ENV = "PUFFPAI_DATA_DIR"

#: The sentence this whole file exists to keep out of a medication turn.
NOT_KEPT = "did not keep it"


@pytest.fixture(autouse=True)
def _no_turn_leaks_out_of_this_file():
    """A turn opened here must not silence an honesty notice in another file.

    The record is thread-local and pytest runs everything on one thread, so
    without this a ``begin_turn`` + calendar write below would leave
    ``health.condition`` in the record for whatever test ran next.
    """
    from puffpai.security import turn_writes
    turn_writes.end_turn()
    yield
    turn_writes.end_turn()


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """api_server wired to a throwaway app-data directory.

    Both caches have to go: ``app_data._data_dir_cache`` (resolves the state
    dir once) and ``api_server._app_data_instance`` (holds an AppData bound to
    whatever dir was resolved first).
    """
    monkeypatch.setenv(_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(app_data_mod, "_data_dir_cache", None)
    import puffpai.api_server as api_server
    monkeypatch.setattr(api_server, "_app_data_instance", None)
    return api_server


def _calendar(tmp_path):
    path = tmp_path / "state" / "app_data" / "calendar.json"
    if not path.exists():
        return {"meds": [], "tasks": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _turn(api, message, model_reply):
    """One served chat turn, in the order the routes run it.

    ``_prepare_user_message`` -> counts snapshot -> ``_verify_and_fix`` (the
    deterministic repair path) -> ``_apply_pii_honesty``. All four chat surfaces
    (/chat/send, /chat/stream, /conversation, /v1/chat/completions) enter at
    ``_prepare_user_message``, which is why the guard and this test both sit there
    rather than in each route.
    """
    llm_text, persist_text, meta = api._prepare_user_message(
        message, source="chat")
    before = api._snapshot_counts(llm_text)
    repaired = api._verify_and_fix(llm_text, model_reply, before)
    return persist_text, meta, api._apply_pii_honesty(repaired, meta)


# ── The four phrasings, on the config the appliance actually runs ──────────

def test_add_insulin_to_my_meds(api, tmp_path):
    """The motivating example.

    The medication is in calendar.json AND the reply says so, naming the
    destination; it never carries "did not keep it".
    """
    persisted, meta, reply = _turn(api, "add insulin to my meds",
                                   "Medication added.")

    meds = [m["name"] for m in _calendar(tmp_path)["meds"]]
    assert meds == ["insulin"], meds
    # The reply and the file agree, and the reply says WHERE.
    assert "insulin" in reply
    assert "calendar's medication list" in reply
    assert NOT_KEPT not in reply
    # The flag-OFF path detects nothing free-text, so
    # nothing redacted out of the turn we persist to history.
    assert meta is None
    assert persisted == "add insulin to my meds"


def test_i_took_my_medicine(api, tmp_path):
    """The turn whose whole reply a health notice would REPLACE.

    Nothing is stored by the deterministic path here (there is no take-med
    repair function; on the box the model's tool call does it), so the property
    under test is that the household's answer survives.
    """
    model_reply = "Recorded: your medicine is marked taken."
    persisted, meta, reply = _turn(api, "I took my medicine", model_reply)

    assert reply == model_reply          # not replaced, not prefixed
    assert NOT_KEPT not in reply
    assert meta is None
    assert persisted == "I took my medicine"


def test_remind_me_to_take_my_blood_pressure_pill_at_8(api, tmp_path):
    """A health-flavoured reminder is a calendar TASK, and it really lands."""
    persisted, meta, reply = _turn(
        api, "remind me to take my blood pressure pill at 8",
        "Reminder set for 8:00 AM.")

    titles = [t["title"] for t in _calendar(tmp_path)["tasks"]]
    assert titles == ["take my blood pressure pill at 8"], titles
    assert NOT_KEPT not in reply
    assert meta is None
    # The medication name is not mangled out of the stored reminder either.
    assert "blood pressure" in titles[0]
    assert "REDACTED" not in persisted


def test_a_health_statement_that_nothing_stores(api, tmp_path):
    """The non-stored control, and the (b) requirement.

    "I have asthma" writes nowhere. The flag-OFF path neither detects nor
    redacts it, and adds no notice on that path; the tagging-at-rest work is
    unaffected either way, because it tags documents, not utterances.
    """
    model_reply = "Noted, thanks for telling me."
    persisted, meta, reply = _turn(api, "I have asthma", model_reply)

    assert reply == model_reply
    assert NOT_KEPT not in reply
    assert meta is None
    assert persisted == "I have asthma"
    assert _calendar(tmp_path)["meds"] == []


# ── The honesty filter itself, driven with the meta the flag-ON path makes ──
#
# The tests above pass partly because the flag-OFF path does not detect free
# text health at all. That is necessary but not sufficient: the filter has to
# hold when a health detection DOES reach _apply_pii_honesty (the vault flag on,
# or any future caller). These drive it directly.

HEALTH_META = {'detected': True, 'types': ['health.condition'], 'vaulted': [],
               'severity': 'high', 'status': 'OK'}
SSN_META = {'detected': True, 'types': ['ssn'], 'vaulted': [],
            'severity': 'critical', 'status': 'OK'}


def test_a_stored_medication_silences_the_notice(api, tmp_path):
    """The core guarantee: no "did not keep it" about something we kept."""
    from puffpai.security import turn_writes

    turn_writes.begin_turn()
    api._get_app_data().cal_add_med("insulin", "1 dose", "all")
    assert "health.condition" in turn_writes.stored_classes()

    assert api._pii_honesty_notice(HEALTH_META) is None
    reply = "Your insulin is on the 8am list."
    assert api._apply_pii_honesty(reply, HEALTH_META) == reply


def test_a_turn_that_stored_nothing_still_gets_the_honest_notice(api):
    """The notice is not weakened: silence is earned by a write, not assumed."""
    from puffpai.security import turn_writes

    turn_writes.begin_turn()
    notice = api._pii_honesty_notice(HEALTH_META)
    assert notice and NOT_KEPT in notice


def test_an_app_write_can_never_silence_an_ssn_notice(api, tmp_path):
    """The filter is bounded by construction.

    It subtracts only the classes ``app_data_tags`` can emit — health.condition,
    health.device_reading, financial.account_balance, child.any — so no app
    write can ever suppress the notice an SSN gets.
    """
    from puffpai.security import turn_writes

    turn_writes.begin_turn()
    api._get_app_data().cal_add_med("insulin", "1 dose", "all")
    notice = api._pii_honesty_notice(SSN_META)
    assert notice and "Social Security number" in notice
    assert NOT_KEPT in notice


def test_a_health_detection_never_replaces_a_reply(api):
    """A medication turn's answer is never discarded for a notice.

    With ``health.condition`` in ``_PII_TYPE_WORD_RE``, "Got it - I've noted
    your medication." would be substituted outright. Even when
    the notice is TRUE, throwing the reply away is the wrong trade on this
    class: the reply is often the only thing the household gets.
    """
    from puffpai.security import turn_writes

    turn_writes.begin_turn()
    claim = "Got it - I've noted your medication."
    out = api._apply_pii_honesty(claim, HEALTH_META)
    assert claim in out                      # kept, not substituted
    assert out.startswith("I spotted a health detail")
    #...while the SSN substitution it was modelled on still works.
    assert api._apply_pii_honesty(
        "Got it - your SSN has been noted.", SSN_META
    ) == api._pii_honesty_notice(SSN_META)


def test_health_and_child_have_no_reply_replacement_pattern(api):
    assert "health.condition" not in api._PII_TYPE_WORD_RE
    assert "child.any" not in api._PII_TYPE_WORD_RE
    #...but they are still SPEAKABLE when the notice is warranted.
    assert "health.condition" in api._PII_SPOKEN_LABEL
    assert "child.any" in api._PII_SPOKEN_LABEL


def test_the_legacy_chat_branch_does_not_scan_free_text_health(api):
    """The flag-OFF chat branch does no free-text health scan, byte for byte.

    Asserted through the public helper rather than by reading the source, so a
    future re-introduction of ``include_health=True`` on this branch fails here.
    """
    result = api._scan_and_redact_message("add my insulin to the meds list")
    assert result["pii_detected"] is False
    assert result["redacted_text"] == "add my insulin to the meds list"
    #...and the detector itself is untouched and still available to callers
    # that want it (the at-rest/scrub paths).
    from puffpai.security.data_classifier import DataClassifier
    assert DataClassifier().scan_for_pii(
        "add my insulin to the meds list", include_health=True).health_conditions


# ── The medication record itself has to be right ───────────────────────────

@pytest.mark.parametrize("message,expected", [
    ("add insulin to my meds", "insulin"),
    ("add my insulin to the meds list", "insulin"),
    ("add amoxicillin to the meds", "amoxicillin"),
    ("add medication insulin", "insulin"),
    ("add a med for lisinopril", "lisinopril"),
    ("set a medication reminder for warfarin", "warfarin"),
])
def test_the_stored_medication_name_is_the_drug_not_the_sentence(
        api, tmp_path, message, expected):
    """A reply that accurately reports a wrong record is still a wrong record.

    "add insulin to my meds" must not store a medication *named* "add insulin
    to my meds", and "set a medication reminder for warfarin" must not store
    one named "reminder for warfarin" (so the prefix list is longest-first).
    """
    _turn(api, message, "Medication added.")
    assert [m["name"] for m in _calendar(tmp_path)["meds"]] == [expected]


def test_a_redacted_message_never_becomes_a_medication_name(api, tmp_path):
    """Reachable only with the vault flag on, and refused rather than stored.

    With ``PUFFPAI_SCRUB_AND_VAULT`` on, the scrubbed text is what reaches the
    routing layer, so the repair path would otherwise write the placeholder
    itself into the household's medication list.
    """
    reply = api._fix_add_med("add my [REDACTED-HEALTH] to the meds list",
                             api._get_app_data())
    assert reply.startswith("!")             # declined, with its own message
    assert "did not add" in reply
    assert _calendar(tmp_path)["meds"] == []


# ── The turn record is per-thread and per-turn ─────────────────────────────

def test_a_background_write_cannot_silence_a_chat_turns_notice(api, tmp_path):
    """The alarm scheduler and the sync poller write on their own threads."""
    import threading
    from puffpai.security import turn_writes

    turn_writes.begin_turn()

    def background():
        # No begin_turn on this thread, so the write is recorded nowhere.
        api._get_app_data().cal_add_med("insulin", "1 dose", "all")

    t = threading.Thread(target=background)
    t.start()
    t.join()

    assert turn_writes.stored_classes() == frozenset()
    assert api._pii_honesty_notice(HEALTH_META)


def test_beginning_a_turn_discards_the_previous_turns_record(api, tmp_path):
    from puffpai.security import turn_writes

    turn_writes.begin_turn()
    api._get_app_data().cal_add_med("insulin", "1 dose", "all")
    assert turn_writes.stored_classes()

    turn_writes.begin_turn()
    assert turn_writes.stored_classes() == frozenset()
    assert api._pii_honesty_notice(HEALTH_META)


def test_the_record_holds_classes_and_paths_only_never_a_value(api, tmp_path):
    """The same discipline as app_data_tags: no household value is reachable."""
    from puffpai.security import turn_writes

    turn_writes.begin_turn()
    api._get_app_data().cal_add_med("insulin", "1 dose", "all")

    blob = repr(turn_writes.stored_classes()) + repr(turn_writes.stored_paths())
    assert "insulin" not in blob.lower()
    # Schema positions, not contents. ``calendar.family[].role`` rides along
    # because the shipped default carries two role-shaped child placeholders —
    # which is itself the point: the record describes the DOCUMENT's shape.
    assert "calendar.meds[]" in turn_writes.stored_paths()
    assert all("." in p for p in turn_writes.stored_paths())
