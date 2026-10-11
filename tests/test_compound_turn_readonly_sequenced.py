"""A read-only sequenced turn must not go to the planner.

Handed to the planner, a sequence of reads is answered with invented prose:

    POST /chat/send {"message": "check whats in the pantry, then tell me the
                                 network status too"}
      -> "Your pantry is currently empty-no items are listed."
    GET /apps/freshtable/data
      -> pantry: [{"name": "ground beef", "quantity": 6, "unit": "pounds",
                   "category": "Protein",...}]

while the SINGLE clause is correct:

    POST /chat/send {"message": "whats in the pantry"}
      -> "Pantry (1 item): ground beef: 6 pounds (Protein)"
      journal: [Chat] Capability read: app_list_pantry

On that path the journal shows ``Compound request -> planner (instant answers
stand down)`` then ``Plan did not satisfy the goal -> legacy agent``, and the
legacy agent's PROSE asserts an empty pantry: an unverified model claim about
household state reaching the user, on the read side.

What this file pins: a read-only sequence goes through the SAME clause
splitter as an action sequence, each clause is answered by its own
instant-answer / capability-read path (the readers that are correct one
clause at a time), and the results are concatenated in the order asked. The
planner is not the path for a sequence of reads.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402
from puffpai import compound_turns as ct  # noqa: E402

#: The compound phrasing under test.
COMPOUND = "check whats in the pantry, then tell me the network status too"

#: A bare-noun shorthand for the same journey.
SHORTHAND = "pantry, then network status"

#: What the single-clause read returns for a pantry holding ONE item. This is
#: the ground truth the compound turn contradicted.
PANTRY_ONE_ITEM = "Pantry (1 item): ground beef: 6 pounds (Protein)"
NETWORK = "Network scan: 19 devices seen, 19 known, 0 unknown."
CLOCK = "It's 3:29 AM on Thursday, September 03."

#: The fabricated sentence. It must never be producible from the
#: fixture below, whatever route the turn takes.
FABRICATION = "Your pantry is currently empty"


class _FakeTool:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def invoke(self, arg):
        self.calls.append(arg)
        return self.reply


class _FakeTools:
    def __init__(self, **tools):
        self.tool_dict = tools


@pytest.fixture
def network_reader(monkeypatch):
    """The registry reader for the network clause, stubbed.

    "tell me the network status too" normalises to "the network status" and
    reaches ``privacy_scan_network`` through the registry, so it does not fall
    through to a bare *read_handler* lambda. The helper tests below assert
    ordering and non-dropping; they just need the reader wired.
    """
    tool = _FakeTool(NETWORK)
    monkeypatch.setattr(api, "_get_assistant_tools",
                        lambda: _FakeTools(privacy_scan_network=tool))
    return tool


@pytest.fixture
def no_network_reader(monkeypatch):
    """A box where the network read is NOT reachable, deterministically."""
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _FakeTools())


@pytest.fixture
def pantry_box(monkeypatch):
    """A box whose pantry holds exactly one item, wired through the REAL
    capability-read layer - not a stubbed instant answer.

    Without a deterministic reader for "tell me the network status too", the
    network half comes back as an automotive LIN bus and generic HTTP status
    codes out of the offline library, while the same question WITHOUT the
    "tell me"/"too" wrapper is answered by its reader in a fraction of a
    second. So the reader is wired here, as it is in production. The
    downstream boundary is stubbed and asserted to receive a CLAUSE and never
    the whole utterance: the planner being handed the whole utterance is the
    defect this file guards against.
    """
    pantry = _FakeTool(PANTRY_ONE_ITEM)
    # The network clause reaches its registry reader, exactly as the
    # single-clause phrasing does (no model). Left unreadable, the clause is
    # answered from the encyclopedia: LIN bus wiring sold as this household's
    # network.
    tools = _FakeTools(app_list_pantry=pantry,
                       privacy_scan_network=_FakeTool(NETWORK))
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: tools)
    monkeypatch.setattr(api, "_augment_persona_with_memory",
                        lambda persona, uid: persona)
    monkeypatch.setattr(api, "_load_persona", lambda: "persona")
    monkeypatch.setattr(api, "_remember_for_user", lambda uid, msg: None)
    monkeypatch.setattr(api, "_requester_for", lambda uid: "owner")

    downstream = []

    def _fake_downstream(message, *a, **k):
        downstream.append(message)
        if ct.is_compound(message):
            pytest.fail(
                "the WHOLE sequenced utterance reached the downstream "
                "answerer: %r. That is the path that fabricated an empty "
                "pantry." % message)
        reply = NETWORK if "network" in message.lower() else             "downstream answer for: " + message
        return {"response": reply, "sources": [], "suggestions": []}

    monkeypatch.setattr(api, "_try_team_chat", _fake_downstream)
    monkeypatch.setattr(api, "_query_ollama_direct",
                        lambda message, *a, **k: _fake_downstream(message))
    return pantry, downstream


# ── splitting ───────────────────────────────────────────────────────────────

def test_the_compound_phrasing_splits_into_two_ordered_read_clauses():
    assert ct.split_clauses(COMPOUND) == [
        "check whats in the pantry", "tell me the network status too"]


def test_the_bare_noun_shorthand_splits_the_same_way():
    assert ct.split_clauses(SHORTHAND) == ["pantry", "network status"]


def test_a_three_read_sequence_keeps_all_three_in_order():
    assert ct.split_clauses(
        "check whats in the pantry, then tell me the network status too, "
        "then what time is it") == [
        "check whats in the pantry",
        "tell me the network status too",
        "what time is it"]


# ── the helper, with no action clause at all ────────────────────────────────

def test_a_read_only_sequence_is_no_longer_handed_to_the_planner(
        network_reader):
    """Returning None (-> planner) for a read-only sequence is the defect."""
    out = api._try_compound_request(COMPOUND, lambda c: "answer to: " + c)
    assert out is not None, "a read-only sequence must be answered here"


def test_both_read_clauses_are_answered_in_the_order_asked(network_reader):
    out = api._try_compound_request(
        COMPOUND,
        lambda c: PANTRY_ONE_ITEM if "pantry" in c else NETWORK)
    assert out.index(PANTRY_ONE_ITEM) < out.index(NETWORK)


def test_three_read_clauses_are_all_answered(network_reader):
    replies = {"check whats in the pantry": PANTRY_ONE_ITEM,
               "tell me the network status too": NETWORK,
               "what time is it": CLOCK}
    out = api._try_compound_request(
        "check whats in the pantry, then tell me the network status too, "
        "then what time is it", lambda c: replies.get(c))
    for part in (PANTRY_ONE_ITEM, NETWORK, CLOCK):
        assert part in out
    assert out.index(PANTRY_ONE_ITEM) < out.index(NETWORK) < out.index(CLOCK)


def test_a_read_clause_is_never_treated_as_an_action(monkeypatch,
                                                    network_reader):
    """No clause here is state-changing, so nothing may be executed."""
    monkeypatch.setattr(
        api, "_try_team_chat",
        lambda *a, **k: pytest.fail("a read clause was run as an action"))
    assert api._try_compound_request(COMPOUND, lambda c: "ok") is not None


def test_an_unanswerable_first_clause_still_stands_down(no_network_reader):
    """Nothing has run yet, so the whole turn is left to the existing path
    rather than half-answered."""
    assert api._try_compound_request(COMPOUND, lambda c: None) is None


def test_an_unanswerable_LATER_clause_is_reported_not_dropped(
        no_network_reader):
    out = api._try_compound_request(
        COMPOUND,
        lambda c: PANTRY_ONE_ITEM if "pantry" in c else None)
    assert PANTRY_ONE_ITEM in out
    assert "tell me the network status too" in out
    assert "ask me that one on its own" in out


# ── the whole chat turn, through the real capability-read layer ─────────────

def test_the_compound_phrasing_reports_the_real_pantry(pantry_box):
    pantry, downstream = pantry_box
    out = api._process_chat_query(COMPOUND)["response"]
    assert PANTRY_ONE_ITEM in out
    assert NETWORK in out
    assert FABRICATION not in out
    assert "empty" not in out.lower()
    assert pantry.calls == [""], "the pantry read ran exactly once"
    # The pantry clause was answered deterministically and never reached the
    # downstream answerer at all.
    assert all("pantry" not in m for m in downstream), downstream


def test_the_single_clause_control_is_unchanged(pantry_box):
    """The clause that was already correct must stay correct."""
    pantry, _ = pantry_box
    out = api._process_chat_query("whats in the pantry")["response"]
    assert out == PANTRY_ONE_ITEM


def test_the_bare_noun_shorthand_answers_both_halves(pantry_box):
    """The bare-noun shorthand. Neither bare noun matches a deterministic
    reader, so both clauses go downstream - but they go SEPARATELY, in order,
    which is the whole point: one utterance per question."""
    _pantry, downstream = pantry_box
    out = api._process_chat_query(SHORTHAND)["response"]
    # The bare noun "network status" IS a declared dispatch phrase and finds
    # its reader, so only "pantry" goes downstream, and it goes on its own,
    # which is the property under test.
    assert downstream == ["pantry"]
    assert out.index("downstream answer for: pantry") < out.index(NETWORK)
    assert FABRICATION not in out


def test_a_three_read_sequence_answers_all_three(pantry_box):
    out = api._process_chat_query(
        "check whats in the pantry, then tell me the network status too, "
        "then what time is it")["response"]
    assert PANTRY_ONE_ITEM in out
    assert NETWORK in out
    # The clock clause is answered by the box's own clock fastpath.
    assert "It's " in out and " on " in out


def test_a_non_sequenced_read_is_untouched(pantry_box):
    """"what's the weather and the forecast" is one request, not two."""
    assert ct.split_clauses("what's the weather and the forecast") == []
