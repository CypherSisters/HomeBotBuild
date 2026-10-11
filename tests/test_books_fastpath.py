"""The phrasings the librarian fastpath accepts — and the ones it must leave
alone.

``_try_books`` is a four-line delegate to :func:`puffpai.books.librarian.handle`
(the SHARED-FILE rule: api_server keeps one call site, the logic lives in
books/), so the phrasings are exercised against ``handle`` directly and the
delegation is checked once.

The decline half matters as much as the accept half: "do we have any Jane
Austen books" keeps the answer
``_try_media_library_query`` already gives it, and the shopping list is not a
shelf.

Run: py -3 -m pytest tests/test_books_fastpath.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))   # the fixture builders

from puffpai import pending_clarifier                              # noqa: E402
from puffpai.books import librarian, library                       # noqa: E402
from test_books_library import (build_catalogue, build_shelves,    # noqa: E402
                                write_popularity)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(library, "_METADATA_DB",
                        build_catalogue(tmp_path / "metadata.db"))
    monkeypatch.setattr(library, "_APP_DB", build_shelves(tmp_path / "app.db"))
    monkeypatch.setattr(library, "_POPULARITY_PATH",
                        write_popularity(tmp_path / "popularity.json"))
    library.forget_last()
    pending_clarifier.reset()
    yield library
    library.forget_last()
    pending_clarifier.reset()


def say(text, client_id="kiosk"):
    return librarian.handle(text, client_id=client_id)


# ── find / browse ───────────────────────────────────────────────────────────

def test_find_a_book_by_title(lib):
    assert "Dracula" in say("find me a book called Dracula")


def test_find_books_by_author(lib):
    said = say("find me books by Jane Austen")
    assert "Pride and Prejudice" in said


def test_what_comics_do_we_have(lib):
    said = say("what comics do we have")
    assert "Walking Dead" in said and "Ultimate X-Men" in said


def test_what_is_in_the_survival_category(lib):
    said = say("what's in the survival category")
    assert "Bushcraft Basics" in said
    assert "Survival & Preparedness" in said


def test_a_category_we_do_not_have_is_said_plainly(lib):
    said = say("what's in the quantum knitting category")
    assert "don't have" in said.lower()


def test_a_title_we_do_not_have_offers_what_we_do(lib):
    said = say("find me a book called The Silmarillion")
    assert "don't have" in said.lower()


# ── recommend ───────────────────────────────────────────────────────────────

def test_recommend_a_category(lib):
    assert "Dracula" in say("recommend a horror book")


def test_what_should_i_read_next(lib):
    said = say("what should I read next")
    assert "Frankenstein" in said            # rank 1 in the sidecar


def test_something_like_a_book_we_have(lib):
    said = say("recommend something like Dracula")
    assert said and "Dracula" not in said.split(":")[1]


def test_recommend_outside_the_library_is_not_ours(lib):
    assert say("recommend a good restaurant") is None


# ── shelves ─────────────────────────────────────────────────────────────────

def test_make_a_shelf(lib):
    assert "Halloween" in say("make a shelf called Halloween")
    assert lib.shelf_name("halloween") == "Halloween"


def test_put_a_book_on_a_shelf_says_exactly_what_it_did(lib):
    said = say("put Dracula on my to-read shelf")
    assert said.startswith("Done")
    assert "Dracula" in said and "To Read" in said
    assert [b.id for b in lib.shelf_books("to read")] == [2]


def test_shelve_it_after_a_search(lib):
    say("find me a book called Dracula")
    said = say("put it on my to-read shelf")
    assert "Dracula" in said
    assert [b.id for b in lib.shelf_books("to-read")] == [2]


def test_move_it_to_favorites(lib):
    say("put Dracula on my to-read shelf")
    said = say("move it to my favorites shelf")
    assert "favorites" in said.lower()
    assert [b.id for b in lib.shelf_books("favorites")] == [2]
    assert lib.shelf_books("to-read") == []


def test_take_it_off_a_shelf(lib):
    say("put Dracula on my to-read shelf")
    said = say("take it off my to-read shelf")
    assert said.startswith("Done") and "Dracula" in said
    assert lib.shelf_books("to read") == []


def test_what_is_on_my_shelf(lib):
    say("put Dracula on my to-read shelf")
    said = say("what's on my to-read shelf")
    assert "Dracula" in said


def test_what_shelves_do_i_have(lib):
    say("make a shelf called Halloween")
    said = say("what shelves do I have")
    assert "Halloween" in said and "0 books" in said


def test_a_small_multi_book_change_is_just_done(lib):
    # Two comics — at or under BULK_LIMIT, so it simply happens.
    said = say("put all my Comics books on my to-read shelf")
    assert said.startswith("Done")
    assert len(lib.shelf_books("to read")) == 2


def test_a_big_umbrella_change_asks_first(lib):
    # Eleven Project Gutenberg books — over BULK_LIMIT, so it asks and waits.
    said = say("put all my Project Gutenberg books on my to-read shelf")
    assert "11 books" in said and "haven't" in said
    assert lib.shelf_books("to read") == []


def test_more_than_five_books_is_a_question_not_an_action(lib, monkeypatch):
    monkeypatch.setattr(librarian, "BULK_LIMIT", 1)
    said = say("put all my Project Gutenberg books on my to-read shelf")
    assert "haven't" in said and "?" in said
    assert lib.shelf_books("to read") == []
    # ...and "yes" on the next turn finishes it (pending_clarifier).
    done = pending_clarifier.resolve("yes please", conversation_id="kiosk")
    assert done and "updated" in done
    assert len(lib.shelf_books("to read")) >= 2


# ── the things it must NOT take ─────────────────────────────────────────────

def test_an_author_question_is_left_to_the_media_path(lib):
    assert say("do we have any Jane Austen books") is None


def test_the_shopping_list_is_not_a_shelf(lib):
    assert say("add milk to the shopping list") is None
    assert say("put eggs on the grocery list") is None


def test_an_unrelated_sentence_falls_through(lib):
    assert say("what's the weather tomorrow") is None
    assert say("turn off the kitchen lights") is None
    assert say("show me the TED talk about surveillance") is None


def test_a_movie_ask_is_not_ours(lib):
    assert say("show me the movie Dracula") is None


# ── the api_server delegate ─────────────────────────────────────────────────

def test_try_books_delegates_and_never_raises(lib):
    from puffpai import api_server
    assert api_server._try_books("what comics do we have", "kiosk")
    assert api_server._try_books("what's the capital of Peru", "kiosk") is None


# ── the librarian never takes a turn the house owns ─────────────────────────

HIJACK_PHRASES = [
    "show me my calendar",
    "show me the weather",
    "show me the thermostat",
    "show me the garden",
    "show me the kitchen",
    "show me the recipe",
    "show me my schedule",
    "pull up the shopping list",
    "show me the cameras",
    "open the firewall page",
]


@pytest.mark.parametrize("phrase", HIJACK_PHRASES)
def test_the_fastpath_declines_house_asks(lib, phrase):
    assert say(phrase) is None


@pytest.mark.parametrize("phrase", [
    "show me Moby Dick",
    "show me the Dracula book",
    "pull up issue 54 of Ultimate X-Men",
    "show me the Walking Dead comic",
    "open Pride and Prejudice",
])
def test_the_fastpath_claims_a_real_book_ask(lib, phrase):
    said = say(phrase)
    assert said and said.startswith("Pulling up")


# ── "all books by <author>" is an AUTHOR ask ────────────────────────────────

def test_all_books_by_an_author_shelves_that_author(lib):
    said = say("add all books by Jane Austen to my to-read shelf")
    assert "Pride and Prejudice" in said
    assert [b.id for b in lib.shelf_books("to read")] == [6]


def test_all_books_by_an_author_never_matches_a_title(lib):
    # "Stephen King" must not drag in a book whose TITLE merely shares words.
    say("add all books by Stephen King to my to-read shelf")
    shelved = [b.title for b in lib.shelf_books("to read")]
    assert shelved == ["The Gunslinger"]


def test_an_author_bulk_over_the_limit_asks_first(lib, monkeypatch):
    monkeypatch.setattr(librarian, "BULK_LIMIT", 0)
    said = say("add all books by Jane Austen to my to-read shelf")
    assert "haven't" in said
    assert lib.shelf_books("to read") == []


# ── "what do I have on <category>" is the librarian's, not the pantry's ─
# Without a librarian match for "what do I have on"
# the turn falls through to the pantry's "what do we have ..." read
# (_CAPREAD_RULES, app_list_pantry), which runs later and answers that it
# found nothing in the pantry.

def test_bk2_what_do_i_have_on_a_category_is_the_librarians(lib):
    said = say("what do I have on survival and preparedness")
    assert said and "Survival & Preparedness" in said
    assert "Bushcraft Basics" in said
    assert "pantry" not in said.lower()


def test_what_do_i_have_on_first_aid_is_the_medicine_category(lib):
    assert library.resolve_category("first aid") == "Medicine & Health"
    said = say("what do I have on first aid")
    assert said and "Medicine & Health" in said
    said = say("what do I have on medicine")
    assert said and "Medicine & Health" in said


def test_bk2_through_the_api_server_delegate(lib):
    from puffpai import api_server
    said = api_server._try_books("what do I have on survival and preparedness",
                                 "kiosk")
    assert said and "Survival & Preparedness" in said


def test_the_librarian_runs_before_the_pantry_read():
    import inspect
    from puffpai import api_server
    src = inspect.getsource(api_server._try_instant_answer)
    assert src.index("_try_books(") < src.index("_try_capability_read(")


PANTRY_PHRASES = [
    "do we have rice",
    "what do we have in the pantry",
    "do we have any eggs",
    "what do we have on hand",
    "what do we have on hand for cooking",
    "what do I have on the shopping list",
    "what do we have on the calendar",
    "what do we have about rice",
    "what do we have on for the kids",     # calendar idiom, not Children's
]


@pytest.mark.parametrize("phrase", PANTRY_PHRASES)
def test_the_librarian_leaves_pantry_asks_alone(lib, phrase):
    assert say(phrase) is None


@pytest.mark.parametrize("phrase", [
    "do we have rice",
    "what do we have in the pantry",
    "do we have any eggs",
])
def test_e1_pantry_asks_still_reach_the_pantry_read(phrase):
    from puffpai import api_server
    for tool, match_re, exclude_re, _needs in api_server._CAPREAD_RULES:
        if match_re.search(phrase) and not (exclude_re is not None
                                            and exclude_re.search(phrase)):
            assert tool == "app_list_pantry"
            return
    raise AssertionError(f"no capability read claimed {phrase!r}")
