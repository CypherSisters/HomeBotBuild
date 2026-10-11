""""search my documents for X" answered directly from the upload index.

Through the agent route, "search my documents for soccer" takes 40 to 60 s:
qwen3 classify ~1 s and plan 4.4 to 6.4 s; "planning run dry (error, 0
executed); falling back"; then a gpt-oss round of 8 to 16 s only to choose
``search_my_documents`` and a second of 22 to 30 s writing the reply. The
reply is correct and local; the route is the cost.

So the tool's own retrieval (``upload_index.search_my_documents``) runs before
the planner and the hits are rendered with no model.
"""

import io
import os
import sys
from contextlib import redirect_stdout

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffpai.agents import document_reads  # noqa: E402


# ── the matcher ─────────────────────────────────────────────────────────────

POSITIVES = [
    # the phrasing the timing above describes
    ("search my documents for soccer", "soccer"),
    ("Search my documents for soccer.", "soccer"),
    ("search my docs for soccer", "soccer"),
    ("search my files for the lease renewal", "the lease renewal"),
    ("search my uploads for soccer", "soccer"),
    ("search my notes for the science fair", "the science fair"),
    ("look in my documents for soccer", "soccer"),
    ("look through my files for the soccer schedule", "the soccer schedule"),
    ("check my documents for soccer, please", "soccer"),
    ("search through all of my documents for the tax receipt",
     "the tax receipt"),
    ("search my uploaded documents for the lease", "the lease"),
    ("search my documents for anything about soccer", "soccer"),
    ("find soccer in my documents", "soccer"),
    ("find the school play times in my docs?", "the school play times"),
    ("look for the pizza recipe in my docs", "the pizza recipe"),
    ("search my documents for everything about taxes", "taxes"),
    ("search my documents for all about taxes", "taxes"),
    ("search my documents for the list of chores", "the list of chores"),
    # a greeting, the bot's name or politeness may come first
    ("can you search my documents for soccer", "soccer"),
    ("please search my docs for the lease", "the lease"),
    ("hey puff pie, search my documents for soccer", "soccer"),
    ("could you please look in my documents for the soccer schedule",
     "the soccer schedule"),
    # "pin" is a whole word only
    ("search my documents for spinach", "spinach"),
    ("search my documents for the pinball league", "the pinball league"),
    ("search for soccer in my uploads", "soccer"),
    ("what's in my documents about soccer", "soccer"),
    ("whats in my documents about soccer", "soccer"),
    ("what is in my files about the car insurance", "the car insurance"),
    ("what do my documents say about soccer", "soccer"),
]


@pytest.mark.parametrize("phrase,topic", POSITIVES)
def test_an_explicit_document_search_yields_its_topic(phrase, topic):
    assert document_reads.match(phrase) == topic


NEGATIVES = [
    # the web: Toolsmith's, and egress-gated
    "search the web for soccer",
    "search online for soccer",
    "search the internet for soccer",
    "search my documents and the web for soccer",
    "google soccer",
    # the knowledge library: _try_knowledge_search's
    "search the knowledge library for soccer",
    "search the library for soccer",
    "search the books for soccer",
    "search my documents and the knowledge base for soccer",
    # email and the calendar have their own readers
    "search my email for soccer",
    "search my inbox for soccer",
    "search my calendar for soccer",
    "search my documents and my calendar for soccer",
    # no topic, or a topic that names nothing: list_my_documents owns these
    "search my documents",
    "search my documents for",
    "search my documents for anything",
    "what's in my documents",
    "show me my documents",
    "what files do I have",
    # no "my": whose documents?
    "search the documents for soccer",
    "search documents for soccer",
    # no search verb
    "my documents about soccer",
    "tell me about soccer",
    "what is soccer",
    # a file the household sent: _try_my_files / _try_document_read own these
    "can you see the file I sent you",
    "read the soccer schedule document",
    # unrelated
    "turn on the kitchen lights",
    "search my movie collection for aliens",
    # a second request riding on the topic: the whole
    # turn keeps its old route so the action is not dropped
    "search my documents for soccer and turn on the lights",
    "look through my files for artists and make a playlist",
    "check my files for the lease and read it to me",
    # no verb list is complete, so any two-part shape stands down (re-verify):
    # a joiner, a sentence break, a run-on command, or a request
    # in front of the search. Noun pairs pay with the slow path, on purpose.
    "search my documents for the lease and print it",
    "search my documents for the lease and summarize it",
    "search my documents for soccer and close the garage",
    "search my documents for the lease and what's the weather",
    "search my documents for the lease. turn on the lights",
    "search my documents for the lease; mute the tv",
    "search my documents for soccer turn on the lights",
    "turn on the lights and search my documents for soccer",
    "search my documents for salt and pepper",
    "search my documents for tom & jerry",
    "search my documents for taxes or receipts",
    # the upload itself is the inventory question _try_my_files answers
    "search my uploads for the file I sent you",
    "search my files for the file I sent yesterday",
    "search my docs for the lease, then remind me to sign it",
    "search my documents for the recipe plus add eggs to the list",
    # media and household things are not documents
    "find the matrix in my files",
    "look for my keys in my files",
    "find the kitchen lights in my files",
    "search my files for movies",
    "search my files for music",
    "search my documents for that song",
    "search my uploads for the episode list",
    # secrets are never looked up here
    "search my documents for my wifi password",
    "search my documents for the garage pin",
    "search my files for my bank login",
    "search my notes for the router credentials",
    "find the passcode in my documents",
]


@pytest.mark.parametrize("phrase", NEGATIVES)
def test_everything_else_falls_through(phrase):
    assert document_reads.match(phrase) is None


def test_a_very_long_utterance_is_a_conversation_not_a_lookup():
    long = "search my documents for " + "a very long topic " * 20
    assert len(long) > document_reads.MAX_LEN
    assert document_reads.match(long) is None


# ── the renderer ────────────────────────────────────────────────────────────

def _rows():
    return [
        {"filename": "Soccer Schedule.pdf", "classification": "document",
         "word_count": 120, "score": 0.81,
         "snippet": "Practice is Tuesday and Thursday at 6 pm at the "
                    "community sports field. Bring shin guards and water."},
        {"filename": "Team Roster.docx", "classification": "document",
         "word_count": 80, "score": 0.52,
         "snippet": "Goalie Sam, forwards Alex, Jordan and Casey, coached by "
                    "Taylor for the fall soccer season."},
    ]


def test_hits_name_the_documents_and_quote_each():
    out = document_reads.render("soccer", _rows())
    lines = out.split("\n")
    assert lines[0] == ("I found 2 documents about soccer: "
                        "Soccer Schedule and Team Roster.")
    assert lines[1] == ('Soccer Schedule says: "Practice is Tuesday and '
                        'Thursday at 6 pm at the community sports field".')
    assert lines[2].startswith('Team Roster says: "Goalie Sam')
    # one quote per document, the first sentence only
    assert "Bring a cap" not in out


def test_hits_are_capped():
    rows = [dict(_rows()[0], filename=f"Doc {i}.pdf",
                 snippet="Soccer practice is Tuesday at 6 pm.")
            for i in range(6)]
    out = document_reads.render("soccer", rows)
    assert out.startswith(f"I found {document_reads.MAX_DOCS} documents")
    assert out.count(" says: ") == document_reads.MAX_DOCS
    assert "Doc 3" not in out


def test_a_long_snippet_is_cut_on_a_word_boundary():
    rows = [{"filename": "Notes.txt", "snippet": "word " * 200}]
    out = document_reads.render("x", rows, quote_chars=60)
    quote = out.split("\n")[1]
    assert quote.endswith('...".')
    assert len(quote) < 100


def test_one_hit_is_singular():
    out = document_reads.render("soccer", _rows()[:1])
    assert out.startswith("I found 1 document about soccer: "
                          "Soccer Schedule.")


def _weak_rows():
    # what a "recipes" search can return: nearest by meaning, none of them
    # about recipes
    return [
        {"filename": "Summer Reading List.pdf", "score": 0.41,
         "snippet": "Week 4: two chapters a night."},
        {"filename": "photo.jpg", "score": 0.38,
         "snippet": "A kitchen counter with a blue bowl."},
        {"filename": "Radio Station.txt", "score": 0.36,
         "snippet": "Stations to try on a long drive."},
    ]


def test_hits_that_never_mention_the_topic_are_not_presented_as_about_it():
    out = document_reads.render("recipes", _weak_rows())
    assert out == ("I didn't find anything that mentions recipes in your "
                   "documents. The closest were Summer Reading List and "
                   "photo.")
    assert "I found" not in out


def test_only_the_hits_that_mention_the_topic_are_listed():
    rows = _weak_rows() + [{"filename": "Pizza Night.pdf", "score": 0.35,
                            "snippet": "Our pizza dough recipe, 3 cups "
                                       "flour and 1 cup water."}]
    out = document_reads.render("recipes", rows)
    assert out.startswith("I found 1 document about recipes: Pizza Night.")
    assert "Fitness" not in out and "photo" not in out


@pytest.mark.parametrize("topic,snippet", [
    ("recipes", "Grandma's recipe for bread."),
    ("the recipe", "Recipes from the fall potluck."),
    ("batteries", "Buy a battery for the smoke alarm."),
    ("the soccer schedule", "Soccer practice moves to 7."),
])
def test_singulars_and_plurals_count_as_mentions(topic, snippet):
    rows = [{"filename": "Notes.txt", "snippet": snippet}]
    assert document_reads.render(topic, rows).startswith("I found 1 document")


def test_a_topic_with_no_content_words_lists_the_hits_as_before():
    rows = [{"filename": "Notes.txt", "snippet": "anything at all"}]
    assert document_reads.render("an", rows).startswith("I found 1 document")


def test_no_hits_is_said_plainly():
    assert (document_reads.render("soccer", []) ==
            "I didn't find anything about soccer in your documents.")


def test_a_search_that_could_not_run_is_not_reported_as_nothing_found():
    out = document_reads.render("soccer", [], failed=True)
    assert out == ("I couldn't search your documents just now, so I can't "
                   "tell you what they say about soccer.")


@pytest.mark.parametrize("rows,failed", [(None, False), ([], False),
                                         (None, True)])
def test_replies_are_plain_words_with_no_dashes(rows, failed):
    rows = _rows() if rows is None and not failed else rows
    out = document_reads.render("soccer", rows, failed=failed)
    assert chr(0x2014) not in out and chr(0x2013) not in out


# ── the retrieval is the tool's own ─────────────────────────────────────────

def test_the_tool_and_the_fastpath_share_one_search(monkeypatch):
    from puffpai import upload_index
    seen = {}

    def _fake(query, max_results=0, min_score=0.0):
        seen.update(query=query, max_results=max_results, min_score=min_score)
        return []

    monkeypatch.setattr(upload_index, "search_uploads", _fake)
    upload_index.search_my_documents("soccer")
    # the values the agent tool has always used
    assert seen == {"query": "soccer", "max_results": 5, "min_score": 0.35}
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "puffpai",
                            "assistant_tools.py"), encoding="utf-8").read()
    assert "search_my_documents as _search" in src


# ── the api_server wiring ───────────────────────────────────────────────────

@pytest.fixture
def api():
    return pytest.importorskip("puffpai.api_server")


@pytest.fixture
def uploads(monkeypatch):
    """The upload index, as the tool reads it, with no ChromaDB behind it."""
    from puffpai import upload_index
    state = {"rows": _rows(), "calls": []}

    def _search(query, max_results=0, min_score=0.0):
        state["calls"].append(query)
        return list(state["rows"])

    monkeypatch.setattr(upload_index, "search_uploads", _search)
    return state


def test_the_fastpath_ignores_a_turn_it_does_not_recognise(api, uploads):
    assert api._try_documents_search("search the web for soccer") is None
    assert api._try_documents_search("turn on the kitchen lights") is None
    assert uploads["calls"] == []            # not even a retrieval pass


def test_the_fastpath_searches_the_topic_not_the_sentence(api, uploads):
    out = api._try_documents_search("search my documents for soccer")
    assert uploads["calls"] == ["soccer"]
    assert out.startswith("I found 2 documents about soccer")


def test_no_hits_is_answered_not_passed_on(api, uploads):
    uploads["rows"] = []
    assert (api._try_documents_search("search my documents for soccer")
            == "I didn't find anything about soccer in your documents.")


def test_a_broken_search_is_said_as_such(api, monkeypatch):
    from puffpai import upload_index

    def _boom(query):
        raise RuntimeError("chromadb is gone")

    monkeypatch.setattr(upload_index, "search_my_documents", _boom)
    out = api._try_documents_search("search my documents for soccer")
    assert out.startswith("I couldn't search your documents just now")


def test_a_fixture_row_is_never_read_out(api, uploads):
    uploads["rows"] = [{"filename": "patient_record.txt",
                        "classification": "unknown", "word_count": 40,
                        "snippet": "Name [REDACTED_NAME] SSN [REDACTED_SSN]",
                        "score": 0.9}]
    out = api._try_documents_search("search my documents for soccer")
    assert "patient_record" not in out
    assert "REDACTED" not in out


def test_a_protected_topic_keeps_its_old_route(api, uploads, monkeypatch):
    # Whatever the vault gate treats as protected is not answered here.
    monkeypatch.setattr(api, "_is_protected_query", lambda *a, **k: True)
    assert api._try_documents_search(
        "search my documents for my passport number") is None
    assert uploads["calls"] == []


def test_a_compound_turn_is_left_to_the_planner(api, uploads):
    assert api._try_documents_search(
        "search my documents for soccer, then tell me the weather") is None


def test_search_my_uploads_is_a_search_not_the_file_list(api, uploads):
    assert api._try_my_files("search my uploads for soccer") is None


# ── routing: nothing slow is consulted, on either surface ──────────────────

W3_PHRASINGS = [
    "search my documents for soccer",        # routes agent
    "search my docs for soccer",             # routes rag
    "search my uploads for soccer",          # routes rag
    "find soccer in my documents",
    "what's in my documents about soccer",
    "look in my documents for soccer",
]


@pytest.fixture
def no_slow_path(api, monkeypatch):
    """Every model path and every network path raises if it is reached."""
    called = []

    def _trap(name):
        def _raise(*a, **k):
            called.append(name)
            raise AssertionError(f"{name} was called")
        return _raise

    for name in ("_try_team_chat", "_route_via_concierge",
                 "_try_composed_plan", "_try_direct_handle",
                 "_query_ollama_direct", "_try_knowledge_search",
                 "_dispatch_toolsmith_web"):
        monkeypatch.setattr(api, name, _trap(name))

    class _Tools:
        # The instant layer borrows the tools object for its alarm-dismiss
        # check; only RUNNING the legacy agent is the slow path.
        run = staticmethod(_trap("legacy agent tools.run"))
        daily_briefing = staticmethod(_trap("tools.daily_briefing"))

    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _Tools())
    return called


@pytest.mark.parametrize("phrase", W3_PHRASINGS)
def test_chat_answers_without_the_planner_or_the_legacy_agent(
        api, uploads, no_slow_path, phrase):
    log = io.StringIO()
    with redirect_stdout(log):
        out = api._process_chat_query(phrase)
    assert no_slow_path == []
    assert out["response"].startswith("I found 2 documents about soccer")
    assert "Web lookup fastpath" not in log.getvalue()
    assert "[Chat] Document search: 2 hit(s)" in log.getvalue()


@pytest.mark.parametrize("phrase", W3_PHRASINGS)
def test_voice_answers_without_the_planner_or_the_legacy_agent(
        api, uploads, no_slow_path, phrase):
    log = io.StringIO()
    with redirect_stdout(log):
        spoken = api._process_voice_query(phrase)
    assert no_slow_path == []
    assert "Web lookup fastpath" not in log.getvalue()
    # Two sentences out loud, every match on the screen.
    assert spoken == (
        "I found 2 documents about soccer: Soccer Schedule and Team "
        "Roster. Soccer Schedule says: \"Practice is Tuesday and Thursday "
        "at 6 pm at the community sports field\"." +
        api.VOICE_TRUNCATION_TAIL_SCREEN)
    assert "Team Roster says:" in api._voice_full_tl.text


def test_voice_no_hits_is_one_plain_sentence(api, uploads, no_slow_path):
    uploads["rows"] = []
    spoken = api._process_voice_query("search my documents for soccer")
    assert no_slow_path == []
    assert spoken == "I didn't find anything about soccer in your documents."



# ── compound requests stand down ──────────────────────────────────────────────

COMPOUND_PHRASES = [
    "search my documents for soccer and turn on the lights",
    "look through my files for artists and make a playlist",
    "check my files for the lease and read it to me",
    "search my documents for the lease and print it",
    "search my documents for soccer and close the garage",
    "search my documents for the lease. turn on the lights",
    "turn on the lights and search my documents for soccer",
]


@pytest.mark.parametrize("phrase", COMPOUND_PHRASES)
def test_a_second_request_keeps_the_turn_on_its_old_route(
        api, uploads, monkeypatch, phrase):
    # These go to the team, which can do both halves.
    team = {"response": "<<team>>", "sources": [], "suggestions": []}
    monkeypatch.setattr(api, "_detect_route", lambda *a, **k: "agent")
    monkeypatch.setattr(api, "_try_instant_answer", lambda *a, **k: None)
    monkeypatch.setattr(api, "_try_compound_request", lambda *a, **k: None)
    monkeypatch.setattr(api, "_try_knowledge_search", lambda *a, **k: None)
    monkeypatch.setattr(api, "_try_capability_gap", lambda *a, **k: None)
    monkeypatch.setattr(api, "_try_team_chat", lambda *a, **k: dict(team))
    assert api._try_documents_search(phrase) is None
    assert api._process_chat_query(phrase)["response"] == "<<team>>"
    assert uploads["calls"] == []


def test_a_topic_with_list_words_still_gets_the_spoken_cap(
        api, uploads, no_slow_path):
    phrase = "search my documents for the list of chores"
    assert document_reads.without_topic(phrase) == "search my documents for"
    # rows that mention the topic, so the full found-list is what gets capped
    uploads["rows"] = [dict(r, snippet="Chores: " + r["snippet"])
                       for r in _rows()]
    spoken = api._process_voice_query(phrase)
    assert no_slow_path == []
    assert spoken.endswith(api.VOICE_TRUNCATION_TAIL_SCREEN)
    assert "Team Roster says:" not in spoken
    assert "Team Roster says:" in api._voice_full_tl.text


def test_without_topic_leaves_an_unclaimed_turn_alone():
    assert (document_reads.without_topic("read me the whole list")
            == "read me the whole list")
    assert (document_reads.without_topic(
        "search my documents for everything about taxes")
        == "search my documents for")


def test_a_protected_search_of_my_uploads_keeps_its_old_answer(
        api, monkeypatch):
    # Base answered this with the upload list; the guard must not change that.
    phrase = "search my uploads for my diagnosis"
    monkeypatch.setattr(api, "_is_protected_query", lambda *a, **k: True)
    monkeypatch.setattr(api, "_files_recent",
                        lambda limit=0: [{"filename": "Budget.pdf",
                                          "indexed_at": "2026-09-20"}])
    monkeypatch.setattr(api, "_try_taste_playlist", lambda *a, **k: None)
    out = api._try_my_files(phrase)
    assert out is not None and "Budget" in out
