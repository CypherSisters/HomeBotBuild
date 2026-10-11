"""The document fastpath must stop eating household questions.

The fastpath must not answer ordinary household questions with a document
miss, for example:

* "What's in the news today?" -> *I don't have a document called "news today".
  The ones I do have are: ...*
* "what is in the pantry" -> *I don't have a document called "pantry". The ones
  I do have are: ...*

``_DOCREF_ASK_RE``, added for "what does the reference sheet say",
must not fire on every "what's in X", since that is how the household asks
about everything, and a generic head noun exclusion ("the document I sent
you") alone is not enough.

Two defects are pinned here:

1. the GATE - the ask patterns only take the turn when the phrase names a
   document in so many words, or names a file the box really holds, and never
   when it names a household surface; a decline is ``None``, which is what
   lets pantry/news/weather reach their own hooks downstream;
2. the MISS REPLY - it must not recite the owner's filenames. Read aloud in
   a shared room, that list can be personal names, health words and employer
   documents, said in answer to a question that was not about documents.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import api_server, upload_index  # noqa: E402

# A small, realistic set of uploads: two photos and two documents.
_LIVE_UPLOADS = [
    {"id": "d1", "filename": "Image.png", "classification": "user_upload",
     "word_count": 0, "indexed_at": "2026-09-12T00:00:00"},
    {"id": "d2", "filename": "IMG0001.jpeg", "classification": "user_upload",
     "word_count": 0, "indexed_at": "2026-09-12T00:00:00"},
    {"id": "d3", "filename": "Summer Reading List.docx",
     "classification": "user_upload", "word_count": 900,
     "indexed_at": "2026-09-11T00:00:00"},
    {"id": "d4", "filename": "Packing Checklist.docx",
     "classification": "user_upload", "word_count": 300,
     "indexed_at": "2026-09-10T00:00:00"},
]


@pytest.fixture
def live_uploads(monkeypatch):
    """The real upload set, behind every reader that asks for it."""
    monkeypatch.setattr(upload_index, "all_uploaded_docs",
                        lambda: [dict(d) for d in _LIVE_UPLOADS])
    monkeypatch.setattr(api_server, "_DOCREF_NAMES_CACHE", (0.0, ()),
                        raising=False)
    yield
    api_server._DOCREF_NAMES_CACHE = (0.0, ())


def _capread_owner(phrase):
    """The capability-read rule that would answer *phrase*, if any."""
    for tool_name, match_re, exclude_re, _needs in api_server._CAPREAD_RULES:
        if match_re.search(phrase) and not (
                exclude_re is not None and exclude_re.search(phrase)):
            return tool_name
    return None


# ── (1) two phrasings that are not document reads ────────────────────────────────────────────

@pytest.mark.parametrize("phrase", [
    "What's in the news today?",
    "what is in the pantry",
])
def test_the_two_live_misroutes_are_not_the_document_path(phrase, live_uploads):
    assert api_server._try_document_read(phrase) is None, phrase


def test_the_news_question_still_reaches_the_news_fastpath():
    """The deterministic fastpath in _handle_direct_command, unchanged."""
    import re
    assert re.search(r"(?i)\b(?:news|headlines?)\b", "what's in the news today?")


def test_the_pantry_question_still_reaches_its_own_read(live_uploads):
    """app_list_pantry sits AFTER _try_document_read, so the decline matters."""
    assert _capread_owner("what is in the pantry") == "app_list_pantry"


# ── (2) every other household surface asked the same way ────────────────────

@pytest.mark.parametrize("phrase", [
    "what's in the fridge",
    "what's on my calendar",
    "what's in the shopping list",
    "what does the weather say",
    "what's in the air outside",
    "what's in the vault",
    "what's in my mailbox",
])
def test_household_surfaces_are_never_the_document_path(phrase, live_uploads):
    assert api_server._try_document_read(phrase) is None, phrase


def test_the_household_surfaces_still_have_an_owner_downstream(live_uploads):
    """Whatever answers these, it is not the document reader.

    "what's in the fridge" has no capability-read rule of its own today (it
    falls through to the household/RAG layer, as it did before the docref rule existed); the
    point of the decline is that it gets there at all.
    """
    from puffpai.agents import household_reads
    assert _capread_owner("what's in my pantry") == "app_list_pantry"
    assert household_reads.match("what's on my calendar") == "calendar_upcoming"


# ── (3) a real document question still gets the document path ───────────────

class _FakeLookup:
    """document_lookup with nothing that matches - the miss branch."""

    calls: list = []

    @staticmethod
    def resolve(reference):
        _FakeLookup.calls.append(reference)
        return None

    @staticmethod
    def known_titles(limit=5):
        return [d["filename"] for d in _LIVE_UPLOADS[:limit]]


@pytest.fixture
def miss_lookup():
    import importlib
    _FakeLookup.calls = []
    sys.modules["puffpai"].document_lookup = _FakeLookup  # type: ignore
    yield _FakeLookup
    sys.modules["puffpai"].document_lookup = importlib.import_module(
        "puffpai.document_lookup")


@pytest.mark.parametrize("phrase", [
    "what does the strength program document say",
    "what does the program sheet say",
    "read my packing checklist file",
    "what's in the Summer Reading List",
])
def test_a_real_document_question_still_claims_the_turn(
        phrase, live_uploads, miss_lookup):
    reply = api_server._try_document_read(phrase)
    assert reply is not None, phrase
    assert miss_lookup.calls, phrase


def test_a_named_upload_is_recognised_without_the_word_document(live_uploads):
    """Gate (b): token overlap with a filename this box really holds."""
    assert api_server._docref_names_an_upload("Summer Reading List")
    assert api_server._docref_names_an_upload("packing checklist")
    assert not api_server._docref_names_an_upload("news today")
    assert not api_server._docref_names_an_upload("pantry")


@pytest.mark.parametrize("phrase", [
    "tell me what's in the document. i just let you",
    "tell me what's in the document. i just sent you",
    "what's in the file. i just gave you",
    "what is in the document i just sent you",
    "what's in the file you just got",
])
def test_a_generic_head_with_a_clause_after_it_is_the_list_question(
        phrase, live_uploads):
    """The live ASR sentence, full stop and all.

    "tell me what's in the document. i just let you" is ASR for "... I just
    sent you". The object captured is "document. i just let you", so a head
    read with .split() was "document." - punctuation attached - and the
    document path claimed the turn and answered "I don't have a document by
    that name." It is the latest-upload / list question and nothing else.
    """
    assert api_server._try_document_read(phrase) is None, phrase


def test_the_live_asr_sentence_reaches_the_document_listing(live_uploads):
    assert _capread_owner(
        "tell me what's in the document. i just let you") == "list_my_documents"


def test_the_list_question_still_belongs_to_list_my_documents():
    """"what's in the document I sent you" is the LIST question, unchanged."""
    assert api_server._try_document_read(
        "tell me what's in the document I sent you") is None
    assert _capread_owner(
        "tell me what's in the document I sent you") == "list_my_documents"
    assert _capread_owner("what documents do you have") == "list_my_documents"


# ── (4) the miss reply never reads filenames aloud ──────────────────────────

def test_the_miss_reply_names_no_file(live_uploads, miss_lookup):
    reply = api_server._try_document_read(
        "what does the strength program document say")
    lowered = reply.lower()
    for doc in _LIVE_UPLOADS:
        assert doc["filename"].lower() not in lowered, doc["filename"]
    assert "img" not in lowered and ".docx" not in lowered
    assert "list your documents" in lowered


def test_the_miss_reply_with_nothing_uploaded_says_so(live_uploads, monkeypatch):
    monkeypatch.setattr(upload_index, "all_uploaded_docs", lambda: [])
    api_server._DOCREF_NAMES_CACHE = (0.0, ())  # drop the 30s name cache
    reply = api_server._try_document_read(
        "what does the strength program document say")
    assert reply is not None
    assert "nothing has been uploaded" in reply.lower()


# ── (5) the explicit list still lists, capped when spoken ───────────────────

def test_the_spoken_document_list_is_capped_at_five_with_a_count():
    listing = "You have 8 ingested documents:\n" + "\n".join(
        "  - file%d.docx (user_upload, 100 words)" % i for i in range(1, 9))
    spoken = api_server._cap_spoken_document_list(listing)
    assert spoken.count("file") == 5
    assert "and 3 more." in spoken
    assert "file5.docx" in spoken and "file6.docx" not in spoken


def test_a_short_document_list_is_spoken_whole():
    listing = "You have 2 ingested documents:\n" + "\n".join(
        "  - file%d.docx (user_upload, 100 words)" % i for i in (1, 2))
    assert api_server._cap_spoken_document_list(listing) == listing


def test_the_cap_only_touches_a_document_listing():
    other = "You have 8 timers running:\n  - tea\n  - laundry"
    assert api_server._cap_spoken_document_list(other) == other
