"""A knowledge search must not be answered with a canned failure.

The failure mode, read-only, on a quiet system::

    search the knowledge library for how to purify drinking water -> 7.56 s
    -> "Sorry, I couldn't complete that one. Could you rephrase?"

Journal, same turn::

    [Chat] Route: agent for: search the knowledge library for how to purify...
    [I] puffpai.agents.engine: engine plan ok in 5.72s (qwen3:30b-a3b)
    [Chat] Team handled: plan -> []
    [W] puffpai.api_server: suppressed chat reasoning-leak: I ran 1 of 1
        planned step(s): 1. archivist.get_memory - done / Everything I
        planned ran.

Deterministic, not model thrash: 7.56 s is the WARM time (a cold agent turn
takes far longer), and the plan reports its own success. The report is a work
log, ``_CHAT_LEAK_MARKERS`` matches "planned step" and "step(s):", and the
guard replaces a "successful" plan with the canned failure.

A plain question ("what is the wingspan of a boeing 747") works because it
carries no agent keyword and lands on ``rag``; "search" does, so the router
sends the library search to the agent route instead.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffpai.agents import knowledge_reads  # noqa: E402


# ── "search the knowledge library for X" ────────────────────────────────────

KNOWLEDGE_PHRASINGS = [
    # the canonical phrasing
    ("search the knowledge library for how to purify drinking water",
     "how to purify drinking water"),
    # the suggestion string the library route itself offers
    ("Search the knowledge base for emergency preparedness",
     "emergency preparedness"),
    ("look up penicillin in the knowledge base", "penicillin"),
    ("what does the knowledge library say about solar panels",
     "solar panels"),
    ("check the knowledge base for how to splint a broken arm",
     "how to splint a broken arm"),
    ("find rainwater harvesting in the reference library",
     "rainwater harvesting"),
    ("search my knowledge library for water filtration, please",
     "water filtration"),
]


@pytest.mark.parametrize("phrase,topic", KNOWLEDGE_PHRASINGS)
def test_an_explicit_library_search_yields_its_topic(phrase, topic):
    assert knowledge_reads.match(phrase) == topic


@pytest.mark.parametrize("phrase", [
    "search my movie library for aliens",
    "search the music library for jazz",
    "what's in my book library",
    "look up that episode in the video library",
])
def test_the_media_shelves_keep_their_own_route(phrase):
    assert knowledge_reads.match(phrase) is None


@pytest.mark.parametrize("phrase", [
    # A plain question: already correct on the rag route, and it
    # must not be dragged onto this layer.
    "what is the wingspan of a boeing 747",
    "turn on the kitchen lights",
    "search the library for emergency preparedness",   # bare, ambiguous
    "which lights are on right now",
])
def test_a_turn_that_is_not_an_explicit_library_search_falls_through(phrase):
    assert knowledge_reads.match(phrase) is None


def test_render_answers_from_the_hits_with_no_model_involved():
    hits = [
        {"text": "Boil water for one minute at a rolling boil to make it "
                 "safe to drink; three minutes above 2,000 m.",
         "library": "wikipedia_en_simple_all_maxi_2025_09",
         "title": "Water purification"},
        {"text": "Household bleach at 2 drops per litre, left 30 minutes, "
                 "disinfects clear water.",
         "library": "survivorlibrary", "title": "Field disinfection"},
    ]
    out = knowledge_reads.render("how to purify drinking water", hits)
    assert "rolling boil" in out
    assert "Water purification" in out
    assert "how to purify drinking water" in out
    # The string this whole fix exists to never emit again.
    assert "couldn't complete that" not in out


def test_render_returns_empty_when_there_is_genuinely_nothing():
    assert knowledge_reads.render("anything", []) == ""
    assert knowledge_reads.render("anything", [{"text": "   "}]) == ""


def test_render_truncates_a_long_passage_on_a_word_boundary():
    hits = [{"text": "word " * 400, "library": "L", "title": "T"}]
    out = knowledge_reads.render("x", hits, passage_chars=100)
    assert "…" in out
    assert len(out) < 600


def test_a_very_long_utterance_is_a_conversation_not_a_lookup():
    long = ("search the knowledge library for "
            + "very long topic " * 40)
    assert len(long) > knowledge_reads.MAX_LEN
    assert knowledge_reads.match(long) is None


# ── the api_server wiring: retrieval decides, the model only phrases ────────

@pytest.fixture
def api():
    return pytest.importorskip("puffpai.api_server")


def _hits():
    return [{"text": "Boil water for one minute at a rolling boil.",
             "library": "wikipedia_en_simple_all_maxi_2025_09",
             "title": "Water purification"}]


PROMPT = "search the knowledge library for how to purify drinking water"


def test_the_fastpath_ignores_a_turn_it_does_not_recognise(api, monkeypatch):
    called = []
    monkeypatch.setattr(api, "_get_rag_context",
                        lambda *a, **k: called.append(a) or {})
    assert api._try_knowledge_search("turn on the kitchen lights") is None
    assert called == []          # not even a retrieval pass


def test_no_fts_hits_leaves_the_turn_exactly_where_it_was(api, monkeypatch):
    monkeypatch.setattr(api, "_get_rag_context",
                        lambda *a, **k: {"hits": [], "sources": []})
    assert api._try_knowledge_search(PROMPT) is None


def test_hits_are_answered_in_prose_when_the_model_is_available(api,
                                                                monkeypatch):
    asked = {}

    def _direct(question, history, persona, enriched=False, **kwargs):
        asked["question"] = question
        return {"response": "Boil it for a minute.", "sources": [],
                "suggestions": []}

    monkeypatch.setattr(api, "_get_rag_context",
                        lambda *a, **k: {"hits": _hits(), "sources": [{"x": 1}]})
    monkeypatch.setattr(api, "_query_ollama_direct", _direct)
    out = api._try_knowledge_search(PROMPT)
    assert out["response"] == "Boil it for a minute."
    # Retrieval and the prose leg both get the TOPIC, not the scaffolding.
    assert asked["question"] == "how to purify drinking water"


@pytest.mark.parametrize("prose", [
    None,                                   # raises
    {"response": "   "},                    # empty
    {"response": ""},
])
def test_a_cold_or_failed_model_still_gets_the_passages(api, monkeypatch,
                                                        prose):
    def _direct(*a, **k):
        if prose is None:
            raise TimeoutError("ollama is reloading the model")
        return prose

    monkeypatch.setattr(api, "_get_rag_context",
                        lambda *a, **k: {"hits": _hits(),
                                         "sources": [{"library": "wiki"}]})
    monkeypatch.setattr(api, "_query_ollama_direct", _direct)
    out = api._try_knowledge_search(PROMPT)
    assert out is not None
    assert "rolling boil" in out["response"]
    assert "couldn't complete that" not in out["response"]
    assert out["sources"] == [{"library": "wiki"}]


def test_a_broken_retrieval_stack_never_raises_into_the_turn(api, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("chromadb is gone")
    monkeypatch.setattr(api, "_get_rag_context", _boom)
    assert api._try_knowledge_search(PROMPT) is None


# ── the second layer: a work log is not an answer ──────────────────────────

def _plan_reply(report):
    return {"ok": True, "handled": True, "reply": report, "intent": "plan",
            "targets": [], "route": "plan", "category": "general",
            "plan": {"executed": 1, "stop_reason": "goal_satisfied",
                     "steps": []}}


def test_a_plan_that_reports_only_a_work_log_falls_back_to_the_legacy_agent(
        api, monkeypatch):
    # The verbatim work-log shape.
    report = ("I ran 1 of 1 planned step(s):\n1. archivist.get_memory — done\n"
              "Everything I planned ran.")
    monkeypatch.setenv("PUFFPAI_CHAT_VIA_TEAM", "1")
    monkeypatch.setattr(api, "_route_via_concierge",
                        lambda *a, **k: _plan_reply(report))
    assert api._try_team_chat(PROMPT, "agent") is None


def test_a_plan_with_a_real_answer_is_still_handed_back(api, monkeypatch):
    monkeypatch.setenv("PUFFPAI_CHAT_VIA_TEAM", "1")
    monkeypatch.setattr(
        api, "_route_via_concierge",
        lambda *a, **k: _plan_reply("The porch light is now on."))
    out = api._try_team_chat("turn on the porch light", "agent")
    assert out is not None
    assert out["response"] == "The porch light is now on."
