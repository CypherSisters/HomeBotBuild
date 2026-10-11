"""The owner's OWN phrasings must reach the web-lookup fastpath.

With web search fully working
(``chat_allow_egress`` on, both doors open, SearXNG answering):

  * *"search the web for the current NVIDIA Jetson Orin Nano price"*
    → ``[Chat] Web lookup fastpath -> toolsmith:web_search``, 0.75 s, real answer.
  * *"can you search the web to when target closes"*
    → ``Route: agent`` → planner → legacy agent → slow ``web_search``.
  * *"can you search the web at all?"*
    → agent → answered from the capability registry's text, which said web
      search "needs a one-time setup first". It did not.

The first worked and the owner's two did not, and the difference was a regex
that insisted on the preposition "for". This file pins the widened set.

The DECLINE corpus leads, for the reason ``test_capread_dispatch.py`` gives:
a router is only as good as the sentences it refuses, and the widened patterns
here sit in front of the household paths. "is the front door open right now"
has the same grammar as "is target open right now" and must never leave the
house.

Run: pytest tests/test_websearch_phrasings.py -q
"""

from __future__ import annotations

import pytest

from puffpai import web_lookup


CITY = "Springfield, IL"


@pytest.fixture(autouse=True)
def _fixed_city(monkeypatch):
    """Pin the household city so an hours query is deterministic."""
    monkeypatch.setattr(web_lookup, "_household_city", lambda: CITY)


# ── the corpus that must NEVER go to the web ────────────────────────────────

#: Every one of these contains a word the widened patterns care about —
#: "search", "open", "close", "hours", "look up", "online" — and in none of
#: them does the household want an outbound request.
DECLINE_CORPUS = [
    # searches of the house's OWN data
    "search my documents for the lease",
    "search the pantry",
    "search the pantry for eggs",
    "search my files for the tax return",
    "search my notes for the wifi password",
    "search the vault for my passport",
    # household things that open and close
    "is the front door open right now",
    "is the garage open",
    "when does the garage door close",
    "is the back door open",
    "is the window open",
    "when does the dishwasher finish",
    "is the fridge open",
    "what time does the safe open",
    # possessives are household subjects
    "when does my alarm go off",
    "what time does my alarm open",
    "is my laptop open",
    "when does our gate close",
    # calendar-shaped, the calendar knows and the web does not
    "when does the meeting close",
    "what time does the movie open",
    "when does school close",
    "is the appointment open",
    # pronouns with no antecedent — the planner has the history, this has none
    "look it up online",
    "look that up on the web",
    "search the web for it",
    # ordinary sentences that merely mention a search engine or the net
    "the google home speaker is offline",
    "turn on the kitchen light",
    "what's on my calendar",
    "play some music",
    "tell me a joke",
]


@pytest.mark.parametrize("message", DECLINE_CORPUS)
def test_declines(message):
    assert web_lookup.parse_web_lookup(message) is None, message


# ── the owner's phrasings must reach the fastpath ───────────────────────────

#: ``(message, expected intent)``. The two real-world phrasings are first.
OWNER_PHRASINGS = [
    # must reach the fastpath, not decline
    ("can you search the web to when target closes", "web_search"),
    # must keep working
    ("search the web for the current NVIDIA Jetson Orin Nano price",
     "web_search"),
]

WIDENED_SEARCH_PHRASINGS = [
    # "can you search the web (to|for|about|on) ..."
    "can you search the web to when target closes",
    "can you search the web for train times",
    "can you search the web about the jetson orin nano",
    "can you search the web on lithium battery storage rules",
    "could you search the web regarding the orin power modes",
    # "search (online|the internet|the web) ..."
    "search online for the jetson orin nano price",
    "search the internet for hurricane season dates",
    "search the web nvidia jetson orin nano price",
    "search the internet nvidia jetson prices",
    # "look X up (online|on the web)"
    "look the jetson orin nano price up online",
    "look up the jetson orin nano price online",
    "look up hurricane season dates on the web",
    # "google ..."
    "google the jetson orin nano price",
    "can you google when target closes",
    # "what does the internet say about ..."
    "what does the internet say about the jetson orin nano",
    "what does the web say about lithium battery storage",
]


@pytest.mark.parametrize("message", WIDENED_SEARCH_PHRASINGS)
def test_widened_phrasings_reach_web_search(message):
    lookup = web_lookup.parse_web_lookup(message)
    assert lookup is not None, message
    assert lookup.intent == "web_search", (message, lookup)
    assert len(lookup.query) >= web_lookup._MIN_QUERY_CHARS, lookup


@pytest.mark.parametrize("message,intent", OWNER_PHRASINGS)
def test_the_two_owner_turns_from_the_box(message, intent):
    lookup = web_lookup.parse_web_lookup(message)
    assert lookup is not None, message
    assert lookup.intent == intent, (message, lookup)


# ── business hours ──────────────────────────────────────────────────────────

HOURS_TURNS = [
    "when does target close",
    "when does target close today",
    "what time does target close",
    "what time does the pharmacy open",
    "is target open right now",
    "is the pharmacy open",
    "what are target's hours",
    "target hours today",
    "when does home depot open tomorrow",
]


@pytest.mark.parametrize("message", HOURS_TURNS)
def test_business_hours_become_a_web_search_with_the_city(message):
    lookup = web_lookup.parse_web_lookup(message)
    assert lookup is not None, message
    assert lookup.intent == "web_search", (message, lookup)
    assert "hours" in lookup.query.lower(), lookup
    assert CITY in lookup.query, lookup


def test_the_owner_turn_is_rewritten_into_an_hours_query():
    """Her actual sentence, end to end: the subject becomes searchable terms."""
    lookup = web_lookup.parse_web_lookup(
        "can you search the web to when target closes")
    assert lookup is not None
    assert lookup.query.lower().startswith("target hours")
    assert CITY in lookup.query


def test_hours_query_without_a_city_does_not_invent_one(monkeypatch):
    """An unset household location degrades to a bare query, never a guess."""
    monkeypatch.setattr(web_lookup, "_household_city", lambda: "")
    assert web_lookup.business_hours_query("when does target close") == \
        "target hours"


def test_household_openables_are_refused_by_the_hours_path():
    """The denylist, asserted directly — not only through parse_web_lookup."""
    for phrase in ("is the front door open", "when does the garage close",
                   "what time does the window open", "is my car open"):
        assert web_lookup.business_hours_query(phrase) == "", phrase


def test_a_whole_sentence_is_not_a_place_name():
    """The five-word cap: a run-on must not become '<sentence> hours <city>'."""
    assert web_lookup.business_hours_query(
        "is the thing i was telling you about earlier today open") == ""


# ── "can you search the web at all?" — the capability question ──────────────

CAPABILITY_QUESTIONS = [
    # the owner's verbatim turn from the box
    "can you search the web at all?",
    "can you search the web",
    "can you search the web at all",
    "do you have internet access",
    "do you have internet access?",
    "can you look things up online",
    "can you look things up online?",
    "do you have access to the internet",
    "do you have internet",
    "are you able to browse the internet",
    "could you access the web",
    "can you do a web search",
    "can you run internet searches",
    "is web search on",
    "is your web search working",
    "can you google things",
    "are you online",
    "are you connected to the internet",
    "can you get online",
    # with the politeness and hedging a person actually types
    "hey puff, can you search the web at all?",
    "so can you search the web right now?",
    "can you search the web these days?",
    "just checking, do you have internet access?",
]


@pytest.mark.parametrize("message", CAPABILITY_QUESTIONS)
def test_capability_questions_are_claimed_and_send_nothing(message):
    assert web_lookup.is_websearch_capability_question(message), message
    lookup = web_lookup.parse_web_lookup(message)
    assert lookup is not None, message
    assert lookup.intent == web_lookup.INTENT_CAPABILITY, (message, lookup)
    assert lookup.query == "" and lookup.url == ""


#: A subject after the question makes it a SEARCH. These must not be absorbed
#: by the capability answer — that would swallow a real request and reply with
#: a status sentence.
NOT_CAPABILITY_QUESTIONS = [
    "can you search the web for train times",
    "can you search the web to when target closes",
    "search the web for the jetson orin nano price",
    "do you have internet access to the router settings",
    "can you look up the jetson price online",
    "google the jetson orin nano price",
    "what's on my calendar",
    "turn on the kitchen light",
]


@pytest.mark.parametrize("message", NOT_CAPABILITY_QUESTIONS)
def test_a_question_with_a_subject_is_a_search_not_a_status(message):
    assert not web_lookup.is_websearch_capability_question(message), message


def test_the_capability_answer_dispatches_nothing(monkeypatch):
    """The whole point: answering "can you search the web" opens no socket."""
    calls = []

    def _explode(intent, payload):
        calls.append(intent)
        raise AssertionError("the capability answer must not dispatch")

    from puffpai import websearch_status
    monkeypatch.setattr(websearch_status, "availability",
                        lambda: (websearch_status.LIVE, ""))
    lookup = web_lookup.parse_web_lookup("can you search the web at all?")
    reply = web_lookup.run_web_lookup(lookup, _explode)
    assert calls == []
    assert reply == websearch_status.answer_live()


# ── the search path still records what it got, without searching again ──────

def test_a_search_records_its_result_count_for_the_status_module():
    from puffpai import websearch_status

    websearch_status.reset_outcomes()
    lookup = web_lookup.WebLookup(intent="web_search", query="jetson orin")
    web_lookup.run_web_lookup(
        lookup, lambda intent, payload: {"ok": True, "results": []})
    assert websearch_status._recent_outcome() is False

    web_lookup.run_web_lookup(lookup, lambda intent, payload: {
        "ok": True,
        "results": [{"title": "t", "url": "https://example.com/", "snippet": "s"}],
    })
    assert websearch_status._recent_outcome() is True
    websearch_status.reset_outcomes()
