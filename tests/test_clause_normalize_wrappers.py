"""Conversational wrappers must not cost a clause its reader.

Without the wrapper trim, a two-clause request goes wrong like this:

    POST /chat/send
      {"message": "check whats in the pantry, then tell me the network status too"}
    -> (48.49 s)
       "Pantry (1 item): ground beef: 6 pounds (Protein)

        The default state of a LIN bus is idle-every slave sits in sleep or
        listening mode until the master pulls the line low... If you're
        checking HTTP traffic on your network, look for 200 OK as success,
        401/403 for authentication issues, and 503 when services are
        overloaded."

The pantry half is real. The network half is fabricated out of the offline
knowledge library and presented as this household's network - and the true half
above it is what makes it credible. Each form alone shows where it breaks:

    "what is the network status" -> reader fires, 0.11 s, 19 devices
    "tell me the network status too" -> falls to rag, 22.80 s, LIN bus

The splitter is right; the MATCHER is what has to tolerate the wrapper.
``find_for_dispatch`` requires whole-utterance equality against a declared
dispatch phrase (the one property that keeps "how do i say 'network status' in
spanish" from running a router scan), so if ``_normalize_utterance`` strips the
ask-PREFIX but not the conversational TAIL, "tell me the network status too"
normalises to "the network status too", which equals nothing.

Two mechanisms, both pinned here:

1. the cause - ``_normalize_utterance`` trims a bounded set of wrappers at BOTH
   ends, in the ONE place every registry matcher already goes through;
2. the backstop - a clause of a sequence that NAMES an appliance read and finds
   no reader says so, instead of being handed onward to the encyclopedia
   (``api_server._compound_read_clause``).

A genuine knowledge clause is untouched and must still be answered, cited, from
the library: "tell me about the moon landing, then whats in the pantry".
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import puffpai.api_server as api  # noqa: E402
from puffpai import capability_registry as reg  # noqa: E402
from puffpai import compound_turns as ct  # noqa: E402

NETWORK = "Network scan: 19 devices seen, 19 known, 0 unknown."
PANTRY = "Pantry (1 item): ground beef: 6 pounds (Protein)"
HOUSE = "House status: all quiet."

#: Eight common conversational wrappers, as format strings.
#: Every one is a pure wrapper: none of them changes WHICH read is asked for.
WRAPPERS = [
    "tell me {}",
    "{} too",
    "{} as well",
    "also {}",
    "can you {}",
    "{} please",
    "{} for me",
    "{} now",
]


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
def box(monkeypatch):
    """Readers wired the way a running box has them, and nothing else."""
    tools = _FakeTools(privacy_scan_network=_FakeTool(NETWORK),
                       app_list_pantry=_FakeTool(PANTRY))
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: tools)
    return tools


# ── 1. the cause: normalisation, in the one place ───────────────────────────

def test_the_tail_wrapped_clause_reaches_its_reader():
    """The second clause of the example above, byte for byte."""
    cap = reg.find_for_dispatch("tell me the network status too")
    assert cap is not None, "normalises to something that equals nothing"
    assert cap.id == "security.network"


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_every_wrapper_keeps_the_network_read(wrapper):
    cap = reg.find_for_dispatch(wrapper.format("the network status"))
    assert cap is not None and cap.id == "security.network", wrapper


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_every_wrapper_keeps_the_vault_read(wrapper):
    cap = reg.find_for_dispatch(wrapper.format("the vault status"))
    assert cap is not None and cap.id == "vault.status", wrapper


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_every_wrapper_keeps_the_house_read(wrapper):
    """The third reader is the household one, which matches by CONTAINMENT and
    therefore tolerates wrappers already - asserted so a future move to
    equality cannot quietly break it."""
    from puffpai.agents import household_reads
    assert household_reads.match(
        wrapper.format("the house status")) == "house_status", wrapper


def test_wrappers_stack():
    """"can you tell me the network status too, please" is still one request."""
    cap = reg.find_for_dispatch(
        "can you tell me the network status too, please")
    assert cap is not None and cap.id == "security.network"


def test_normalisation_is_the_single_place_the_matcher_goes_through():
    """The trim lives in the shared normaliser, not in per-rule regexes: the
    raw dispatch phrases round-trip through the SAME function the wrappers are
    stripped in."""
    for cap in reg.all_capabilities():
        for phrase in getattr(cap, "dispatch_phrases", ()) or ():
            assert reg.find_for_dispatch(phrase) is not None, phrase


def test_no_declared_phrase_can_be_trimmed_into_another():
    """The widening's safety argument, mechanised: a dispatch phrase that
    CONTAINED a trim word could be normalised into a different phrase and
    silently dispatch the wrong read."""
    for cap in reg.all_capabilities():
        for phrase in getattr(cap, "dispatch_phrases", ()) or ():
            assert reg._normalize_utterance(phrase) == phrase.lower(), (
                f"{phrase!r} is altered by the trim list")


# ── 2. no over-matching: the routing decline corpus ─────────────────────────

def test_the_existing_decline_corpus_still_declines():
    """Run the decline corpus against the WIDER trim. Widening a normaliser is
    exactly how a matcher starts answering sentences that only mention it."""
    from test_capread_dispatch import DECLINE_CORPUS

    offenders = [(s, reg.find_for_dispatch(s).id) for s in DECLINE_CORPUS
                 if reg.find_for_dispatch(s) is not None]
    assert not offenders, "these ordinary sentences dispatched:\n" + "\n".join(
        f"  {s!r} -> {c}" for s, c in offenders)


@pytest.mark.parametrize("sentence", [
    # The trim words in their ORDINARY, load-bearing senses.
    "how do i say 'network status' in spanish too",
    "what does network status mean, also",
    "my sister's network status now",
    "print the vault status for me",
    "email me the network status please",
    "is the vault locked in the heist movie too",
    "network statustoo",
    "network statusnow",
])
def test_wrapper_words_do_not_become_a_way_in(sentence):
    assert reg.find_for_dispatch(sentence) is None, sentence


# ── 3. the backstop: a sequence clause never reaches the encyclopedia ───────

def test_the_two_clause_example_answers_both_halves_for_real(box):
    """The network clause must be answered by its READER, not by the handler.

    The pantry clause legitimately uses the handler: its reader is one of the
    containment-matched ``_CAPREAD_RULES``, reached through the full
    ``_try_instant_answer`` chain rather than through the registry, so it is
    not brittle to a wrapper and is not what the trim covers.
    """
    def _handler(clause):
        if "network" in clause:
            pytest.fail("the network clause fell past its reader: %r" % clause)
        return PANTRY

    out = api._try_compound_request(
        "check whats in the pantry, then tell me the network status too",
        _handler)
    assert PANTRY in out
    assert NETWORK in out
    assert out.index(PANTRY) < out.index(NETWORK)


def test_a_named_read_with_no_reader_declines_instead_of_guessing(monkeypatch):
    """The clause NAMES an appliance read; the reader is unreachable. It must
    say so rather than borrow the library's confidence."""
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _FakeTools())
    out = api._try_compound_request(
        "check whats in the pantry, then tell me the network status too",
        lambda clause: PANTRY if "pantry" in clause else "LIN bus idle state")
    assert PANTRY in out
    assert "LIN bus" not in out
    assert "don't have a reader" in out
    assert "tell me the network status too" in out


def test_a_genuine_knowledge_clause_still_goes_to_knowledge(box):
    """The negative. A sequence clause that is NOT about the appliance keeps
    its ordinary path, so the cited library answer survives."""
    seen = []

    def _handler(clause):
        seen.append(clause)
        return ("Apollo 11 landed on 1969-07-20. [source: Wikipedia]"
                if "moon" in clause else PANTRY)

    out = api._try_compound_request(
        "tell me about the moon landing, then whats in the pantry", _handler)
    assert "tell me about the moon landing" in seen, (
        "the knowledge clause was intercepted by the appliance backstop")
    assert "Apollo 11" in out and "[source: Wikipedia]" in out
    assert out.index("Apollo 11") < out.index(PANTRY)


@pytest.mark.parametrize("clause", [
    "tell me about the moon landing",
    "how do i purify drinking water",
    "what is the wingspan of a boeing 747",
    "explain photosynthesis",
    "who was julius caesar",
    "how do i change a tire",
])
def test_knowledge_clauses_are_not_appliance_subjects(clause):
    assert reg.mentions_dispatch_subject(clause) is None, clause


@pytest.mark.parametrize("clause,cap_id", [
    ("tell me the network status too", "security.network"),
    ("get me the network status for me", "security.network"),
    ("the vault status as well", "vault.status"),
    ("what is in my vault", "vault.secrets"),
])
def test_appliance_clauses_are_recognised_as_subjects(clause, cap_id):
    subject = reg.mentions_dispatch_subject(clause)
    assert subject is not None and subject.id == cap_id, clause


def test_mentions_subject_is_never_wired_to_dispatch():
    """Containment names a SUBJECT; it must never invoke a tool. The Spanish
    translation question is the case that proves the two are different."""
    q = "how do i say 'network status' in spanish"
    assert reg.mentions_dispatch_subject(q) is not None
    assert reg.find_for_dispatch(q) is None


def test_splitter_leaves_the_wrapper_on_the_clause():
    """The splitter only splits; trimming wrappers is the matcher's job."""
    assert ct.split_clauses(
        "check whats in the pantry, then tell me the network status too") == [
        "check whats in the pantry", "tell me the network status too"]
    assert ct.split_clauses(
        "tell me about the moon landing, then whats in the pantry") == [
        "tell me about the moon landing", "whats in the pantry"]
