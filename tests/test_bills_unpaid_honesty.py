""""are there any unpaid bills" -> false incapacity claim.

If ``are there any unpaid bills right now`` falls through to the agent, the
model answers "I don't have a way to pull up a current list of your unpaid
bills" without a tool call. That is false: the control phrase "any bills due
this month", which matches the ``bills_status`` trigger's "bills... due" word
order, dispatches correctly and answers honestly from the real records.

Two guards here:

* the trigger is word-order-free, so every "unpaid bills" ordering is claimed,
  not only the noun-before-state-word one.
* a bills-domain question in HOUSEHOLD-RECORD framing that survives the exclude
  and the knowledge veto reaches the bills reader regardless of phrasing, so
  the worst case is the real (possibly empty) records rather than an invented
  incapacity.

The fallback gate must not be ``capability_registry.find(text).id ==
"bills.manage"``. That function is a substring ranker and ``bills.manage``
declares the keyword ``"bill"``, so any string containing those four letters
would be classified as the household's ledger: general-knowledge and civics
questions would be answered with the real pending amounts, while genuine
record questions that the ranker scores elsewhere or not at all would still be
refused. Both lists are pinned below.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.agents import household_reads  # noqa: E402

#: The primary phrasing first, then its variants.
POSITIVE = [
    "are there any unpaid bills right now",
    "are there any unpaid bills",
    "any unpaid bills?",
    "unpaid bills",
    "which bills are overdue",
    "what bills haven't we paid",
    "what bills havent we paid",
    "did we pay the electric bill",
    "any bills due this month",
    "are any bills past due",
    "what bills are still outstanding",
    "is the water bill overdue",
    "do we owe money on bills",
    "how much do we owe",
]

#: Every other thing called a bill. None of these may reach the bills reader.
NEGATIVE = [
    "bill of rights",
    "what is the bill of rights?",
    "the bill in congress",
    "tell me about the bill in congress",
    "Bill's birthday",
    "when is Bill's birthday?",
    "electric bill of a heat pump",
    "what is the electric bill of a heat pump?",
    "who is Bill Gates",
    "put a billboard up",
]

#: The WRITE reading of the same words keeps its own path.
WRITES = [
    "add a bill for the electric company",
    "mark the electric bill paid",
    "pay the electric bill",
    "delete the water bill",
]


@pytest.mark.parametrize("phrase", POSITIVE)
def test_bills_question_reaches_the_bills_reader(phrase):
    assert household_reads.match(phrase) == "bills_status", phrase


@pytest.mark.parametrize("phrase", NEGATIVE)
def test_other_bills_are_not_household_bills(phrase):
    assert household_reads.match(phrase) != "bills_status", phrase


@pytest.mark.parametrize("phrase", WRITES)
def test_write_intent_is_not_claimed_as_a_read(phrase):
    assert household_reads.match(phrase) != "bills_status", phrase


class _EmptyBills:
    def cal_list_bills(self, status="pending"):
        return f"No {status} bills."


class _SomeBills:
    def cal_list_bills(self, status="pending"):
        return "1 pending bill (total $84.00):\nElectric: $84.00 due Sep 20"


def test_empty_records_say_what_was_read_not_i_cant():
    """A miss states the store it just read. A shrug is what the model
    rewrote into "I don't have a way to pull up your bills"."""
    said = household_reads.bills_status(app=_EmptyBills())
    low = said.lower()
    assert "no unpaid bills recorded" in low
    assert "read your bill records" in low
    for denial in ("i don't have a way", "i can't", "i cannot",
                   "unable to", "no access"):
        assert denial not in low, said


def test_real_records_are_returned_verbatim():
    said = household_reads.bills_status(app=_SomeBills())
    assert "Electric: $84.00" in said


def test_unpaid_bills_right_now_is_answered_from_the_store():
    """End to end through the module's own dispatcher, with an injected store."""
    said = household_reads.answer("are there any unpaid bills right now",
                                  app=_SomeBills())
    assert said is not None
    assert "Electric: $84.00" in said


def test_registry_fallback_catches_an_unlisted_phrasing():
    """The trigger table is a phrasing list and will always have holes; the
    registry guard is what makes a hole harmless."""
    assert household_reads._bills_registry_fallback(
        "how are we doing on bills this month") == "bills_status"
    assert household_reads._bills_registry_fallback(
        "what is the bill of rights?") is None


# ── the registry fallback is not a substring match ──────────────────────────
#
# Every phrasing below is a general-knowledge, advice or civics question that
# merely contains the letters "bill". Answering any of them with the
# household's real ledger ("2 pending bills (total $203.50)...") leaks real
# amounts into a question that never asked about the household. Each of these
# must reach RAG, never the ledger.
KNOWLEDGE_NEGATIVE = [
    "how does a bill become law",
    "can the president veto a bill",
    "what is a spending bill",
    "what does the infrastructure bill do",
    "who writes a bill",
    "how many votes does a bill need",
    "what happens when a bill passes",
    "is the bill going to pass",
    "how do utility bills work",
    "how do i lower my electric bill",
    "what is the average water bill",
    "should we pay more taxes on bills",
    "bill of rights",
    "what should we do about our bills",
    "can we deduct bills on taxes",
    "do we pay too much in taxes on bills",
]

# Genuine record questions a registry-ranker gate would refuse, handing them to
# the model to invent an incapacity. "what's the situation with bills" ranks as
# introspection.capabilities; "is there anything owed" scores nothing.
RECORD_POSITIVE_UNRANKED = [
    "what's the situation with bills",
    "is there anything owed",
    "our bills",
    "run down the bills for me",
    "give me the bill totals",
    "what's coming due",
    "how much do we owe",
    "how much are our utility bills due this month",
]


@pytest.mark.parametrize("phrase", KNOWLEDGE_NEGATIVE)
def test_a_knowledge_question_about_bills_never_reads_the_ledger(phrase):
    assert household_reads.match(phrase) != "bills_status", phrase
    assert household_reads._bills_registry_fallback(phrase) is None, phrase


@pytest.mark.parametrize("phrase", RECORD_POSITIVE_UNRANKED)
def test_household_record_framing_reaches_the_bills_reader(phrase):
    assert household_reads.match(phrase) == "bills_status", phrase


@pytest.mark.parametrize("phrase", KNOWLEDGE_NEGATIVE)
def test_a_knowledge_question_returns_no_amounts(phrase):
    """End to end with a populated store: not one real amount may escape."""
    said = household_reads.answer(phrase, app=_SomeBills())
    assert said is None, (phrase, said)


def test_the_fallback_no_longer_consults_the_registry_ranker():
    """Pinned so a future edit cannot quietly re-introduce the leak: the gate
    is deterministic and local, and `find` is a ranker, not a classifier."""
    from puffpai import capability_registry
    leaky = "how does a bill become law"
    cap = capability_registry.find(leaky)
    assert cap is not None and cap.id == "bills.manage", (
        "the ranker still says bills.manage for a civics question; that is "
        "exactly why the fallback must not gate on it")
    assert household_reads._bills_registry_fallback(leaky) is None
