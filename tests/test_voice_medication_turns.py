"""The medication turns, deterministic and without the drug name.

The failure modes:

* "I took my [medication] this morning" -> 25 seconds on the agent graph,
  then "I couldn't find a medication named [X]... Did you add it?", reading
  the drug name out loud twice, which the privacy rules forbid.
* "set up a daily reminder to take my [medication] in the morning" -> asked
  for a dose and a time, then "I don't have access to a task-reminder tool",
  which is false.

Cause of the first: keeping the trailing "this morning" as part of the name,
so nothing matches. Of the second: no deterministic owner, so the answer
depends on the tool menu the model was handed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402
from puffpai import chat_activity  # noqa: E402

#: A stand-in drug name. Never a real prescription in a test fixture.
MED_NAME = "Zyloprax"


class FakeAppData:
    """Just the three calls the medication fastpath makes."""

    def __init__(self, meds=None):
        self.meds = list(meds or [])
        self.taken = []
        self.scheduled = []

    def get_data(self, doc):
        return {"meds": self.meds} if doc == "calendar" else {}

    def cal_take_med(self, med_id):
        self.taken.append(med_id)
        return "Recorded."

    def cal_set_med_schedule(self, med_id, next_time=None, freq=None):
        self.scheduled.append((med_id, next_time, freq))
        return {"id": med_id}


def _med(name=MED_NAME, label=None, med_id="m1"):
    return {"id": med_id, "name": name, "label": label, "freq": "daily",
            "next": "8:00 AM", "vaulted": bool(label)}


@pytest.fixture()
def app(monkeypatch):
    store = FakeAppData()
    monkeypatch.setattr(api, "_get_app_data", lambda: store)
    return store


# ── the dose log ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("phrase", [
    "I took my %s this morning" % MED_NAME,
    "I took my %s" % MED_NAME,
    "log my %s" % MED_NAME,
    "I had my %s this morning" % MED_NAME,
    "mark my %s as taken" % MED_NAME,
])
def test_a_dose_is_logged_in_one_step(app, phrase):
    app.meds = [_med()]
    reply = api._try_med_log(phrase)
    assert reply == "Logged your medication as taken."
    assert app.taken == ["m1"]


def test_the_reply_never_says_the_medication_name(app):
    app.meds = [_med()]
    reply = api._try_med_log("I took my %s this morning" % MED_NAME)
    assert MED_NAME.lower() not in reply.lower()


def test_a_labelled_medication_is_confirmed_by_its_label(app):
    app.meds = [_med(label="morning pills")]
    reply = api._try_med_log("I took my %s this morning" % MED_NAME)
    assert reply == "Logged morning pills as taken."
    assert MED_NAME.lower() not in reply.lower()


def test_the_generic_noun_logs_the_only_medication_on_file(app):
    app.meds = [_med()]
    assert api._try_med_log("I took my medication this morning") == \
        "Logged your medication as taken."
    assert app.taken == ["m1"]


def test_two_medications_and_a_generic_noun_asks_which(app):
    app.meds = [_med(med_id="m1"), _med(name="Other", med_id="m2")]
    reply = api._try_med_log("I took my medication this morning")
    assert "which one" in reply.lower()
    assert app.taken == []


# ── nothing on file (meds = 0) ──────────────────────────────────────────────

def test_an_unknown_medication_is_answered_once_without_the_name(app):
    reply = api._try_med_log("I took my %s this morning" % MED_NAME)
    assert reply == ("I don't have that medication on file — add it in the "
                     "Meds app and I'll track it from there.")
    assert MED_NAME.lower() not in reply.lower()
    assert app.taken == []


def test_the_generic_noun_with_an_empty_list_says_the_same_thing(app):
    assert api._try_med_log("I took my medicine") is not None
    assert "Meds app" in api._try_med_log("I took my medicine")


@pytest.mark.parametrize("phrase", [
    "I took my dog for a walk",
    "I took my kids to school",
    "I took my car in this morning",
    "what medications do I have",
    "how many meds am I on",
])
def test_the_rule_declines_everything_that_is_not_a_dose(app, phrase):
    assert api._try_med_log(phrase) is None


# ── the schedule ────────────────────────────────────────────────────────────

def test_a_reminder_with_a_time_is_scheduled_and_confirmed_by_label(app):
    app.meds = [_med(label="morning pills")]
    reply = api._try_med_log(
        "set up a daily reminder to take my %s at 8 am" % MED_NAME)
    assert reply == "Set — I'll remind you about morning pills every day at 8:00 AM."
    assert app.scheduled == [("m1", "8:00 AM", "daily")]
    assert MED_NAME.lower() not in reply.lower()


def test_a_reminder_with_no_time_uses_the_default_and_says_so(app):
    app.meds = [_med()]
    reply = api._try_med_log(
        "remind me to take my %s every morning" % MED_NAME)
    assert "8:00 AM" in reply and "default" in reply
    assert app.scheduled == [("m1", "8:00 AM", "daily")]
    assert MED_NAME.lower() not in reply.lower()


def test_a_spoken_time_is_understood(app):
    app.meds = [_med()]
    api._try_med_log("set up a daily reminder to take my %s at seven pm"
                     % MED_NAME)
    assert app.scheduled == [("m1", "7:00 PM", "daily")]


def test_an_unknown_medication_still_leaves_the_reminder_to_the_task_rule(app):
    """_try_add_task owns "remind me to take my X at 8"."""
    assert api._try_med_log(
        "remind me to take my blood pressure pill at 8") is None


# ── where it sits, and what it is credited to ───────────────────────────────

def test_the_turn_is_credited_to_the_medication_capability():
    assert chat_activity.FASTPATH_CAPABILITY["_try_med_log"] == "health.meds"


# ── the ask must not write, and the answer must ────────────────────────────
#
# "set up a daily reminder for my medication in the morning" must get the three
# questions (name / dose / time) WITHOUT creating a medication row called "up a
# daily reminder for my medication in the morning", dose "1 dose", next
# "8:00 AM": a garbled sentence stored as a prescription while the same breath
# asks what the prescription is.

REMINDER_ASK = "set up a daily reminder for my medication in the morning"


class WritingAppData(FakeAppData):
    """FakeAppData plus the write the ask must never make."""

    def __init__(self, meds=None):
        super().__init__(meds)
        self.added = []

    def cal_add_med(self, name, dose, assigned_to, freq='daily',
                    next_time='8:00 AM', **kwargs):
        self.added.append({"name": name, "dose": dose, "to": assigned_to,
                           "freq": freq, "next": next_time})
        self.meds.append({"id": "m%d" % len(self.added), "name": name,
                          "dose": dose, "next": next_time, "label": None})
        return self.meds[-1]


@pytest.fixture()
def writing_app(monkeypatch):
    store = WritingAppData()
    monkeypatch.setattr(api, "_get_app_data", lambda: store)
    return store


@pytest.fixture(autouse=True)
def _no_open_questions():
    from puffpai import pending_clarifier
    pending_clarifier.reset()
    yield
    pending_clarifier.reset()


def test_the_med_reminder_asks_and_writes_nothing(writing_app):
    reply = api._try_med_schedule_ask(REMINDER_ASK)
    assert reply
    assert "called" in reply.lower() and "dose" in reply.lower() \
        and "time" in reply.lower()
    assert "haven't written" in reply.lower()
    assert writing_app.added == [], "a row was written while asking"
    assert writing_app.meds == []


def test_the_answer_completes_the_reminder_with_a_read_back(writing_app):
    from puffpai import turn_context

    turn_context.begin("conv-remind")
    assert api._try_med_schedule_ask(REMINDER_ASK)
    reply = api._try_instant_answer("lisinopril, 10 milligrams, 7 AM",
                                    conversation_id="conv-remind")
    assert reply, "the answer fell through to routing"
    assert len(writing_app.added) == 1, writing_app.added
    row = writing_app.added[0]
    assert row["name"] == "lisinopril"
    assert row["dose"] == "10 milligrams"
    assert row["next"] == "7:00 AM"
    # A medication is never read back by its drug name.
    assert "lisinopril" not in reply.lower()
    assert "7:00 AM" in reply and "every day" in reply


def test_half_an_answer_re_asks_and_still_writes_nothing(writing_app):
    from puffpai import turn_context

    turn_context.begin("conv-remind-2")
    api._try_med_schedule_ask(REMINDER_ASK)
    reply = api._try_instant_answer("lisinopril",
                                    conversation_id="conv-remind-2")
    assert reply and "what time" in reply.lower()
    assert writing_app.added == []
    #...and the question is still open, so the time finishes it.
    done = api._try_instant_answer("7 AM", conversation_id="conv-remind-2")
    assert done and len(writing_app.added) == 1


def test_the_repair_path_never_stores_a_reminder_sentence_as_a_drug(
        writing_app):
    """The garbled reminder-sentence row, refused at its source."""
    assert api._fix_add_med(REMINDER_ASK, writing_app) is None
    assert writing_app.added == []


def test_a_medication_already_on_file_is_scheduled_instead_of_asked_about(app):
    app.meds = [_med()]
    reply = api._try_med_schedule_ask(REMINDER_ASK)
    assert reply and app.scheduled == [("m1", "8:00 AM", "daily")]
    assert MED_NAME.lower() not in reply.lower()


@pytest.mark.parametrize("said", [
    "what medications am I on",
    "delete my medication reminder",
    "remind me to take my %s at 8" % MED_NAME,
    "add a reminder to do the dishes",
])
def test_the_ask_declines_everything_that_is_not_its_shape(app, said):
    assert api._try_med_schedule_ask(said) is None
