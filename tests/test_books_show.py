"""«Show me that book» — the payload, the URL, and who wins a bare title.

The dashboard owns ``/dashboard/media/books?book=<id>&fmt=<fmt>``; this
file pins our half of that contract: the id is Calibre's, the format is the
preferred one the book actually HAS, a MOBI-only book carries no ``fmt`` at
all, and the movie/book tie-break is decided by the noun the person said.

Run: py -3 -m pytest tests/test_books_show.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))   # the fixture builders

from puffpai import dashboard_display as dd                        # noqa: E402
from puffpai.books import library, show                            # noqa: E402
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
    yield library
    library.forget_last()


# ── the payload ─────────────────────────────────────────────────────────────

def test_payload_is_a_same_origin_url_kind(lib):
    hit = show.resolve_book("show me the book Moby Dick")
    assert hit["kind"] == dd.KIND_URL
    assert hit["url"] == "/dashboard/media/books?book=1&fmt=epub"
    assert hit["title"] == "Moby Dick; Or, The Whale"
    assert hit["meta"]["book_id"] == 1
    assert hit["meta"]["source"] == "calibre"
    assert hit["meta"]["media_type"] == "book"


def test_comic_format_and_series_metadata(lib):
    hit = show.resolve_book("show me the Walking Dead comic")
    assert hit["url"] == "/dashboard/media/books?book=3&fmt=cbz"
    assert hit["meta"]["media_type"] == "comic"
    assert hit["meta"]["series"] == "The Walking Dead"


def test_mobi_only_book_opens_its_detail_page(lib):
    hit = show.resolve_book("pull up the book The Gunslinger")
    assert hit["url"] == "/dashboard/media/books?book=8"
    assert "fmt=" not in hit["url"]


def test_numbered_issue_of_a_series(lib):
    hit = show.resolve_book("pull up issue 54 of Ultimate X-Men")
    assert hit["meta"]["book_id"] == 4
    assert hit["meta"]["series_index"] == 54.0


# ── movie vs book ───────────────────────────────────────────────────────────

def test_a_movie_framed_ask_is_never_a_book(lib):
    assert show.resolve_book("show me the movie Dracula") is None
    assert show.resolve_book("I want to watch Dracula") is None


def test_a_book_framed_ask_is_ours(lib):
    assert show.is_book_ask("show me the comic Walking Dead") is True
    assert show.is_book_ask("show me the movie Walking Dead") is False
    assert show.is_book_ask("show me Casablanca") is False


def test_a_bare_title_needs_a_confident_catalogue_match(lib):
    # Two content words and a whole-title match: ours.
    assert show.resolve_book("show me Moby Dick")["meta"]["book_id"] == 1
    # ONE word is not enough to take a turn off the rest of the house.
    assert show.resolve_book("show me Dracula") is None
    assert show.resolve_book("show me the Dracula book")["meta"]["book_id"] == 2
    # A topic, not a title: the knowledge shelves keep it.
    assert show.resolve_book("show me the history of whaling in Nantucket") is None


# ── the hijack table (the HIGH finding) ─────────────────────────────────────
#
# Every DECLINED phrase below matched a real title in the 60k catalogue and was
# answered with a book instead of by the handler that owns it: "show me my
# calendar" → A Calendar of Sonnets, "show me the weather" → Weather Proverbs.
# The fixture contains those exact trap titles, so these are real declines and
# not accidents of a thin fixture.

DECLINED = [
    "show me my calendar",
    "show me the weather",
    "show me the thermostat",
    "show me the garden",
    "show me the kitchen",
    "show me the recipe",
    "show me my schedule",
    "pull up the shopping list",
    "show me the cameras",
    "show me the lights in the living room",
]

CLAIMED = [
    ("show me Moby Dick", 1),
    ("show me the Dracula book", 2),
    ("pull up issue 54 of Ultimate X-Men", 4),
    ("show me the Walking Dead comic", 3),
    ("open Pride and Prejudice", 6),
    ("show me Frankenstein by Mary Shelley", 7),
]


@pytest.mark.parametrize("phrase", DECLINED)
def test_the_house_keeps_its_own_asks(lib, phrase):
    assert show.resolve_book(phrase) is None


@pytest.mark.parametrize("phrase,book_id", CLAIMED)
def test_a_real_book_ask_is_claimed(lib, phrase, book_id):
    hit = show.resolve_book(phrase)
    assert hit is not None, phrase
    assert hit["meta"]["book_id"] == book_id


def test_a_possessive_before_one_noun_is_never_a_title(lib):
    assert show._possessive_single_noun("show me my calendar") is True
    assert show._possessive_single_noun("show me my Dracula book") is False


def test_house_words_are_recognized(lib):
    assert show.names_a_house_thing("what about the thermostat") is True
    assert show.names_a_house_thing("Moby Dick") is False


# ── anaphora ────────────────────────────────────────────────────────────────

def test_show_it_to_me_uses_the_remembered_book(lib):
    lib.remember_last("kiosk", lib.find("Dracula"))
    hit = show.resolve_book("show it to me", client_id="kiosk")
    assert hit["meta"]["book_id"] == 2


def test_the_second_one(lib):
    listed = lib.find(comics_only=True)
    lib.remember_last("kiosk", listed)
    hit = show.resolve_book("show me the second one", client_id="kiosk")
    assert hit["meta"]["book_id"] == listed[1].id


def test_anaphora_with_nothing_remembered_falls_through(lib):
    assert show.resolve_book("show it to me", client_id="kiosk") is None


# ── the resolution order in dashboard_display ───────────────────────────────

def test_books_come_after_app_pages(lib):
    # "my library" is the Knowledge app page and must keep winning.
    hit = dd.resolve_display("pull up my library")
    assert hit["url"] == "/dashboard/knowledge"


def test_books_come_before_the_knowledge_fallback(lib, monkeypatch):
    def _boom(*_a, **_k):                 # the shelves must never be consulted
        raise AssertionError("knowledge fallback should not run")
    monkeypatch.setattr(dd, "_search_upload_docs", _boom)
    hit = dd.resolve_display("show me the book Frankenstein")
    assert hit["url"] == "/dashboard/media/books?book=7&fmt=epub"


def test_show_query_sets_the_book_display(lib, tmp_path, monkeypatch):
    monkeypatch.setattr(dd, "_STATE_PATH", str(tmp_path / "display.json"))
    item = dd.show_query("show me the book Dracula", source="voice")
    assert item["url"] == "/dashboard/media/books?book=2&fmt=epub"
    assert item["title"] == "Dracula"
    assert dd.get_display()["url"] == item["url"]
