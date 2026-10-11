"""H2: "where did you find that" cites the last turn and never runs a search.

asked immediately after a correctly cited library
answer about water purification, it ran a NEW retrieval on the words of the
question and read out three Wiktionary entries — "findes", "finds", "finding
fault" — 45 seconds later.
"""

import pytest

from puffpai import turn_context
from puffpai.agents import source_citation


@pytest.fixture(autouse=True)
def _clean():
    turn_context.reset()
    turn_context.begin("conv-1")
    yield
    turn_context.reset()


WATER_SOURCES = [
    {"library": "Wikipedia", "title": "Water purification", "relevance": 0.81},
    {"library": "survivorlibrary", "title": "Field Water Treatment",
     "relevance": 0.74},
]


# ── the trigger table ────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Where did you find that information?",
    "where did you find that",
    "where did you get that",
    "where'd you get that?",
    "Where is that from?",
    "what's your source",
    "What was the source?",
    "what were your sources?",
    "how do you know that",
    "What did you base that on?",
    "cite that",
    "can you cite your sources",
])
def test_every_live_phrasing_is_a_source_question(text):
    assert source_citation.is_source_question(text)


@pytest.mark.parametrize("text", [
    "where did you put the shopping list",
    "how do you know how to purify water",   # a topic, not a citation ask
    "what's the source of the leak in the roof",
    "find me a salmon recipe",
    "how do I purify water?",
    "what time is it",
    "where is the living room thermostat set to",
])
def test_declines_everything_that_is_not_a_source_question(text):
    assert not source_citation.is_source_question(text)
    assert source_citation.handle(text, {"sources": WATER_SOURCES}) is None


# ── after a cited RAG turn ───────────────────────────────────────────────

def test_after_a_cited_turn_it_names_the_libraries():
    turn_context.note_sources(WATER_SOURCES)
    reply = source_citation.handle("Where did you find that information?",
                                   turn_context.last_answer("conv-1"))
    assert "Water purification" in reply
    assert "Field Water Treatment" in reply
    assert "knowledge library" in reply
    # The Wiktionary failure was a NEW search; nothing here may look one up.
    assert "Wiktionary" not in reply


def test_no_more_than_four_titles_are_spoken():
    turn_context.note_sources([{"title": f"Doc {n}"} for n in range(8)])
    reply = source_citation.reply_for(turn_context.last_answer("conv-1"))
    assert "Doc 3" in reply and "Doc 4" not in reply


def test_repeat_titles_are_spoken_once():
    turn_context.note_sources([{"title": "Water purification"},
                               {"title": "Water purification"},
                               {"title": "Field Water Treatment"}])
    titles = source_citation.spoken_titles(
        turn_context.last_answer("conv-1")["sources"])
    assert titles == ["Water purification", "Field Water Treatment"]


def test_a_source_without_a_title_falls_back_to_its_library():
    turn_context.note_sources([{"library": "survivorlibrary"}])
    reply = source_citation.reply_for(turn_context.last_answer("conv-1"))
    assert "survivorlibrary" in reply


# ── after a fastpath turn ────────────────────────────────────────────────

def test_after_a_fastpath_turn_it_says_so_and_names_the_store():
    turn_context.note_answer("fastpath", "_try_shopping_add")
    reply = source_citation.reply_for(turn_context.last_answer("conv-1"))
    assert reply.startswith("That last answer didn't come from the library")
    assert "direct reading of" in reply
    assert "pantry" in reply.lower() or "shopping" in reply.lower()


def test_an_unmapped_fastpath_still_answers_honestly():
    turn_context.note_answer("fastpath", "_try_time_answer")
    reply = source_citation.reply_for(turn_context.last_answer("conv-1"))
    assert "didn't come from the library" in reply
    assert "time answer" in reply or "your own data on this box" in reply


# ── with no history ──────────────────────────────────────────────────────

def test_with_no_previous_turn_it_says_there_is_nothing_to_cite():
    assert source_citation.reply_for(None) == source_citation.NO_PREVIOUS
    reply = source_citation.handle("what's your source",
                                   turn_context.last_answer("conv-1"))
    assert reply == source_citation.NO_PREVIOUS


def test_an_expired_record_is_not_cited():
    turn_context.note_sources(WATER_SOURCES)
    record = turn_context.last_answer("conv-1")
    assert record is not None
    record["ts"] = 0
    turn_context._ANSWERS["conv-1"]["ts"] = 0
    assert turn_context.last_answer("conv-1") is None


def test_another_conversation_is_never_cited():
    turn_context.note_sources(WATER_SOURCES, conversation_id="conv-other")
    assert turn_context.last_answer("conv-1") is None


# ── the dispatcher wiring ────────────────────────────────────────────────

def test_the_fastpath_is_registered_and_never_retrieves():
    import inspect

    from puffpai import api_server

    src = inspect.getsource(api_server._try_instant_answer)
    assert "_try_source_citation" in src
    body = inspect.getsource(
        getattr(api_server._try_source_citation, "__wrapped__",
                api_server._try_source_citation))
    for forbidden in ("_get_rag_context", "_try_knowledge_search",
                      "_query_ollama_direct"):
        assert forbidden not in body, (
            f"{forbidden} in the source-citation fastpath re-opens H2: the "
            "question's own words become a new search.")


def test_it_runs_before_the_late_guards_and_after_the_household_reads():
    from tests.test_instant_answer_precedence import _call_order

    order = [name for _lineno, name in _call_order()]
    assert order.index("_try_household_read") < order.index("_try_source_citation")
    assert order.index("_try_source_citation") < order.index("_try_chitchat")
    assert order.count("_try_source_citation") == 1
