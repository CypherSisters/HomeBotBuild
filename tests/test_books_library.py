"""The librarian's hands, against tiny REAL sqlite databases.

Every fixture below is built from the schema Calibre's ``metadata.db`` actually
uses (books / authors / tags / series / data /
identifiers / custom_columns) and Calibre-web's ``app.db`` (shelf /
book_shelf_link / user, including ``admin`` at role 479 and ``Guest`` at 32).
Nothing here touches the household's real libraries: the point is that a shelf
we write is a shelf Calibre-web can read back, so the INSERT column set and the
timestamp format are asserted, not assumed.

Run: py -3 -m pytest tests/test_books_library.py -q
"""

from __future__ import annotations

import re
import sqlite3
import time

import pytest

from puffpai.books import library


# ── fixtures: the two databases, in miniature ───────────────────────────────

#: (id, title, author, tags, series, series_index, formats, gutenberg id)
CATALOGUE = [
    (1, "Moby Dick; Or, The Whale", "Herman Melville",
     ["Project Gutenberg", "Adventure & Westerns"],
     "", None, ["EPUB", "MOBI"], "2701"),
    (2, "Dracula", "Bram Stoker",
     ["Project Gutenberg", "Horror & Gothic"], "", None, ["EPUB"], "345"),
    (3, "The Walking Dead #54", "Robert Kirkman", ["Comics"],
     "The Walking Dead", 54.0, ["CBZ"], ""),
    (4, "Ultimate X-Men #54", "Brian Michael Bendis", ["Comics"],
     "Ultimate X-Men", 54.0, ["CBR"], ""),
    (5, "Bushcraft Basics", "Tom Ranger",
     ["My Library", "Survival & Preparedness"], "", None, ["PDF"], ""),
    (6, "Pride and Prejudice", "Jane Austen",
     ["Project Gutenberg", "Romance"], "", None, ["EPUB"], "1342"),
    (7, "Frankenstein", "Mary Shelley",
     ["Project Gutenberg", "Science Fiction"], "", None, ["EPUB"], "84"),
    (8, "The Gunslinger", "Stephen King",
     ["My Library", "Fantasy & Folklore"], "", None, ["MOBI"], ""),
    # ── the traps ───────────────────────────────────────────────────────────
    # Every one of these is a REAL title in the household's 60k catalogue (or
    # the same shape as one). In a library this big almost any household noun
    # is also a book, which is how "show me my calendar" was answered with a
    # book of sonnets. They are in the fixture so the decline is TESTED.
    (9, "A Calendar of Sonnets", "Helen Hunt Jackson",
     ["Project Gutenberg", "Poetry"], "", None, ["EPUB"], "2151"),
    (10, "Weather Proverbs", "H. H. C. Dunwoody",
     ["Project Gutenberg", "Science & Nature"], "", None, ["EPUB"], "2152"),
    (11, "The Kitchen Garden", "Emily Holt",
     ["Project Gutenberg", "Homestead & Cooking"], "", None, ["EPUB"], "2153"),
    (12, "Recipes Tried and True", "Ladies Aid Society",
     ["Project Gutenberg", "Homestead & Cooking"], "", None, ["EPUB"], "2154"),
    (13, "The Thermostat and Its Uses", "A Heating Engineer",
     ["Project Gutenberg", "Technology & Engineering"], "", None, ["EPUB"], "2155"),
    (14, "Schedule of Rates", "Various",
     ["Project Gutenberg", "Society & Politics"], "", None, ["EPUB"], "2156"),
    (15, "The Shopping List Murders", "A Mystery Writer",
     ["Project Gutenberg", "Mystery & Detective"], "", None, ["EPUB"], "2157"),
    # A literal percent sign, for the LIKE-wildcard escaping.
    (16, "50% Off Murder", "Sherry Harris",
     ["My Library", "Mystery & Detective"], "", None, ["EPUB"], ""),
    (17, "Dr_cula Redux", "A Typo",
     ["My Library", "Horror & Gothic"], "", None, ["EPUB"], ""),
]

SUBJECTS = {1: "Whaling -- Fiction", 2: "Vampires -- Fiction"}


def build_catalogue(path, *, with_subjects: bool = True) -> str:
    """A miniature Calibre metadata.db. ``with_subjects`` False = curation has
    not created the custom column yet (subject search silently unavailable)."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT,
                            series_index REAL DEFAULT 1.0, timestamp TEXT);
        CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER,
                                         author INTEGER);
        CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER,
                                      tag INTEGER);
        CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER,
                                        series INTEGER);
        CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT,
                           name TEXT);
        CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER,
                                  type TEXT, val TEXT);
        CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT,
                                     name TEXT, datatype TEXT);
    """)
    authors: dict = {}
    tags: dict = {}
    series_ids: dict = {}
    for bid, title, author, tag_list, series, index, formats, gid in CATALOGUE:
        conn.execute("INSERT INTO books (id, title, series_index) VALUES (?,?,?)",
                     (bid, title, index if index is not None else 1.0))
        aid = authors.setdefault(author, len(authors) + 1)
        conn.execute("INSERT OR IGNORE INTO authors (id, name) VALUES (?,?)",
                     (aid, author))
        conn.execute("INSERT INTO books_authors_link (book, author) VALUES (?,?)",
                     (bid, aid))
        for tag in tag_list:
            tid = tags.setdefault(tag, len(tags) + 1)
            conn.execute("INSERT OR IGNORE INTO tags (id, name) VALUES (?,?)",
                         (tid, tag))
            conn.execute("INSERT INTO books_tags_link (book, tag) VALUES (?,?)",
                         (bid, tid))
        if series:
            sid = series_ids.setdefault(series, len(series_ids) + 1)
            conn.execute("INSERT OR IGNORE INTO series (id, name) VALUES (?,?)",
                         (sid, series))
            conn.execute("INSERT INTO books_series_link (book, series) VALUES (?,?)",
                         (bid, sid))
        for fmt in formats:
            conn.execute("INSERT INTO data (book, format, name) VALUES (?,?,?)",
                         (bid, fmt, title))
        if gid:
            conn.execute("INSERT INTO identifiers (book, type, val) VALUES (?,?,?)",
                         (bid, "url", f"http://www.gutenberg.org/{gid}"))
    if with_subjects:
        conn.execute("INSERT INTO custom_columns (id, label, name, datatype) "
                     "VALUES (1, 'subjects', 'Subjects', 'text')")
        conn.executescript("""
            CREATE TABLE custom_column_1 (id INTEGER PRIMARY KEY, value TEXT);
            CREATE TABLE books_custom_column_1_link (id INTEGER PRIMARY KEY,
                                                     book INTEGER, value INTEGER);
        """)
        for n, (bid, subject) in enumerate(SUBJECTS.items(), start=1):
            conn.execute("INSERT INTO custom_column_1 (id, value) VALUES (?,?)",
                         (n, subject))
            conn.execute("INSERT INTO books_custom_column_1_link (book, value) "
                         "VALUES (?,?)", (bid, n))
    conn.commit()
    conn.close()
    return str(path)


def build_shelves(path) -> str:
    """Calibre-web's app.db, schema-exact for the three tables we touch."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE user (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(64), email VARCHAR(120), role SMALLINT,
            password VARCHAR, kindle_mail VARCHAR(120), locale VARCHAR(2),
            sidebar_view INTEGER, default_language VARCHAR(3),
            denied_tags VARCHAR, allowed_tags VARCHAR,
            denied_column_value VARCHAR, allowed_column_value VARCHAR,
            view_settings JSON, kobo_only_shelves_sync INTEGER,
            UNIQUE (name), UNIQUE (email));
        CREATE TABLE shelf (
            id INTEGER NOT NULL, uuid VARCHAR, name VARCHAR,
            is_public INTEGER, user_id INTEGER, kobo_sync BOOLEAN,
            created DATETIME, last_modified DATETIME, PRIMARY KEY (id),
            FOREIGN KEY(user_id) REFERENCES user (id));
        CREATE TABLE book_shelf_link (
            id INTEGER NOT NULL, book_id INTEGER, "order" INTEGER,
            shelf INTEGER, date_added DATETIME, PRIMARY KEY (id),
            FOREIGN KEY(shelf) REFERENCES shelf (id));
        INSERT INTO user (id, name, role) VALUES (1, 'admin', 479);
        INSERT INTO user (id, name, role) VALUES (2, 'Guest', 32);
    """)
    conn.commit()
    conn.close()
    return str(path)


def write_popularity(path, data=None) -> str:
    import json
    data = data if data is not None else {
        "84": {"rank": 1, "lcc": "PR"}, "2701": {"rank": 2, "lcc": "PS"},
        "1342": {"rank": 4, "lcc": "PR"}, "345": {"rank": 30, "lcc": "PR"}}
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """The library module pointed at fixture databases, memory cleared."""
    monkeypatch.setattr(library, "_METADATA_DB",
                        build_catalogue(tmp_path / "metadata.db"))
    monkeypatch.setattr(library, "_APP_DB", build_shelves(tmp_path / "app.db"))
    monkeypatch.setattr(library, "_POPULARITY_PATH",
                        write_popularity(tmp_path / "popularity.json"))
    library.forget_last()
    yield library
    library.forget_last()


# ── find ────────────────────────────────────────────────────────────────────

def test_exact_title_beats_the_rest(lib):
    hits = lib.find("Dracula")
    assert hits and hits[0].title == "Dracula"
    assert hits[0].authors == ["Bram Stoker"]
    assert hits[0].gutenberg_id == "345"


def test_articles_and_punctuation_do_not_matter(lib):
    assert lib.find("the walking dead")[0].id == 3
    assert lib.best_match("Moby Dick").id == 1


def test_prefix_ranks_above_contains(lib):
    hits = lib.find("gunslinger")
    assert hits[0].title == "The Gunslinger"


def test_author_search_is_the_authors_table(lib):
    hits = lib.find(author="Kirkman")
    assert [b.id for b in hits] == [3]


def test_comics_carry_series_and_index(lib):
    comic = lib.get(4)
    assert comic.series == "Ultimate X-Men" and comic.series_index == 54.0
    assert comic.is_comic and comic.best_format == "CBR"


def test_format_preference_and_mobi_only(lib):
    assert lib.get(1).best_format == "EPUB"      # EPUB wins over MOBI
    assert lib.get(8).best_format == ""          # MOBI-only → detail page


def test_my_library_tag_is_personal(lib):
    assert lib.get(5).is_mine is True
    assert lib.get(2).is_mine is False
    assert [b.id for b in lib.find(mine_only=True)] == [5, 8, 16, 17]


# ── categories, spoken loosely ──────────────────────────────────────────────

@pytest.mark.parametrize("spoken,expected", [
    ("sci fi", "Science Fiction"),
    ("scifi", "Science Fiction"),
    ("science fiction", "Science Fiction"),
    ("mysteries", "Mystery & Detective"),
    ("kids books", "Children's & Young Readers"),
    ("survival", "Survival & Preparedness"),
    ("graphic novels", "Comics"),
    ("cookbooks", "Homestead & Cooking"),
    ("gardening", "Homestead & Cooking"),
    ("farming", "Homestead & Cooking"),
    ("westerns", "Adventure & Westerns"),
    ("sea stories", "Adventure & Westerns"),
    ("folklore", "Fantasy & Folklore"),
    ("myth", "Fantasy & Folklore"),
])
def test_loose_category_forms(spoken, expected):
    assert library.resolve_category(spoken) == expected


def test_unknown_category_is_none():
    assert library.resolve_category("quantum knitting") is None


def test_category_browse_uses_the_tag(lib):
    assert [b.id for b in lib.find(category="scifi")] == [7]
    assert [b.id for b in lib.find(category="survival")] == [5]
    assert lib.find(category="quantum knitting") == []


def test_comics_only_filter(lib):
    assert sorted(b.id for b in lib.find(comics_only=True)) == [3, 4]


# ── subjects (the custom column that may not exist yet) ─────────────────────

def test_subject_search_uses_the_custom_column(lib):
    assert [b.id for b in lib.find(subject="Whaling")] == [1]


def test_subject_search_silently_unavailable_without_the_column(tmp_path,
                                                                monkeypatch):
    monkeypatch.setattr(library, "_METADATA_DB", build_catalogue(
        tmp_path / "plain.db", with_subjects=False))
    assert library.find(subject="Whaling") == []
    assert library.find("Dracula")[0].id == 2      # everything else still works


# ── recommend ───────────────────────────────────────────────────────────────

def test_recommendations_follow_the_popularity_sidecar(lib):
    picks = lib.recommend(limit=3)
    assert [b.gutenberg_id for b in picks] == ["84", "2701", "1342"]


def test_recommendations_are_deterministic_without_a_sidecar(tmp_path, lib,
                                                             monkeypatch):
    monkeypatch.setattr(library, "_POPULARITY_PATH",
                        str(tmp_path / "missing.json"))
    first = [b.id for b in lib.recommend(limit=4, seed=7)]
    second = [b.id for b in lib.recommend(limit=4, seed=7)]
    assert first == second
    assert first != [b.id for b in lib.recommend(limit=4, seed=99)]


def test_recommend_within_a_category(lib):
    assert [b.id for b in lib.recommend(category="horror")] == [2, 17]


def test_recommend_like_a_book_never_recommends_that_book(lib):
    # Dracula is the only Horror & Gothic book we have, so the filter widens
    # rather than answering "nothing" — but never back to Dracula itself.
    picks = lib.recommend(like_book_id=2, limit=3)
    assert picks and 2 not in [b.id for b in picks]


def test_read_shelf_is_excluded(lib):
    lib.create_shelf("Read")
    lib.add_to_shelf(7, "Read")
    assert [b.gutenberg_id for b in lib.recommend(limit=2)] == ["2701", "1342"]


# ── shelves (Calibre-web's own) ─────────────────────────────────────────────

def _rows(path, sql):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def test_created_shelf_matches_calibre_webs_own_row_shape(lib):
    assert lib.create_shelf("To Read")["ok"] is True
    rows = _rows(lib.app_db_path(), "SELECT * FROM shelf")
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "To Read"
    assert row["is_public"] == 1
    assert row["user_id"] == 1                      # the first admin, not Guest
    assert re.fullmatch(r"[0-9a-f-]{36}", row["uuid"])
    for stamp in (row["created"], row["last_modified"]):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}", stamp)


def test_shelf_names_match_loosely(lib):
    lib.create_shelf("To Read")
    assert lib.shelf_name("to read") == "To Read"
    assert lib.shelf_name("TO-READ") == "To Read"
    assert lib.shelf_name("halloween") is None
    assert lib.create_shelf("to-read")["action"] == "shelf_exists"


def test_add_move_and_remove_one_book(lib):
    added = lib.add_to_shelf(2, "to read")
    assert added["ok"] and added["action"] == "add"
    assert added["book"] == "Dracula" and added["shelf"] == "To Read"
    link = _rows(lib.app_db_path(), 'SELECT book_id, "order", shelf FROM book_shelf_link')
    assert link == [{"book_id": 2, "order": 1, "shelf": 1}]

    assert lib.add_to_shelf(2, "To-Read")["action"] == "already_on_shelf"
    assert [b.title for b in lib.shelf_books("to read")] == ["Dracula"]
    assert lib.shelves() == [("To Read", 1)]

    moved = lib.move(2, "to read", "Favorites")
    assert moved["ok"] and moved["action"] == "move"
    assert [b.id for b in lib.shelf_books("favorites")] == [2]
    assert lib.shelf_books("to read") == []

    removed = lib.remove_from_shelf(2, "favorites")
    assert removed["ok"] and removed["action"] == "remove"
    assert lib.shelf_books("favorites") == []
    assert lib.remove_from_shelf(2, "favorites")["action"] == "not_on_shelf"


def test_order_increments_per_shelf(lib):
    lib.add_to_shelf(1, "to read")
    lib.add_to_shelf(2, "to read")
    orders = [r["order"] for r in _rows(
        lib.app_db_path(), 'SELECT "order" FROM book_shelf_link ORDER BY id')]
    assert orders == [1, 2]


def test_a_book_the_catalogue_does_not_have_is_never_shelved(lib):
    result = lib.add_to_shelf(999, "to read")
    assert result["ok"] is False
    assert "catalogue" in result["detail"]
    assert _rows(lib.app_db_path(), "SELECT * FROM book_shelf_link") == []


def test_writes_never_raise_when_the_database_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(library, "_METADATA_DB",
                        build_catalogue(tmp_path / "metadata.db"))
    monkeypatch.setattr(library, "_APP_DB", str(tmp_path / "gone.db"))
    result = library.add_to_shelf(1, "to read")
    assert result["ok"] is False and result["action"] == "add"
    assert library.shelves() == []


# ── "it" / "the second one" ─────────────────────────────────────────────────

def test_last_result_memory_is_per_client(lib):
    lib.remember_last("kiosk", lib.find("Dracula"))
    assert [b.id for b in lib.last("kiosk")] == [2]
    assert lib.last("phone") == []


def test_last_result_expires(lib, monkeypatch):
    lib.remember_last("kiosk", lib.find("Dracula"))
    later = time.time() + library.LAST_TTL + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert lib.last("kiosk") == []


# ── two-phase ranking over a category too big to page through ───────────────
#
# The bug this pins: ranking a CAPPED slice by popularity ranks the lowest
# Calibre ids. So the fixture puts the best-known books at the HIGHEST ids —
# exactly where a "first 800 rows by id" pool would never look.

BIG_CATEGORY = "Mystery & Detective"
BIG_COUNT = 1500


def build_big_catalogue(path) -> str:
    """1,500 mysteries; the 10 most popular are the LAST ids in the table."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT,
                            series_index REAL DEFAULT 1.0);
        CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER,
                                         author INTEGER);
        CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_tags_link (id INTEGER PRIMARY KEY, book INTEGER,
                                      tag INTEGER);
        CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_series_link (id INTEGER PRIMARY KEY, book INTEGER,
                                        series INTEGER);
        CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT,
                           name TEXT);
        CREATE TABLE identifiers (id INTEGER PRIMARY KEY, book INTEGER,
                                  type TEXT, val TEXT);
        CREATE TABLE custom_columns (id INTEGER PRIMARY KEY, label TEXT,
                                     name TEXT, datatype TEXT);
        INSERT INTO authors (id, name) VALUES (1, 'A Writer');
        INSERT INTO tags (id, name) VALUES (1, 'Mystery & Detective');
        INSERT INTO tags (id, name) VALUES (2, 'Poetry');
    """)
    for n in range(1, BIG_COUNT + 1):
        conn.execute("INSERT INTO books (id, title) VALUES (?,?)",
                     (n, f"Mystery Number {n:04d}"))
        conn.execute("INSERT INTO books_authors_link (book, author) VALUES (?,1)",
                     (n,))
        conn.execute("INSERT INTO books_tags_link (book, tag) VALUES (?,1)", (n,))
        conn.execute("INSERT INTO data (book, format, name) VALUES (?,?,?)",
                     (n, "EPUB", f"mystery{n}"))
        conn.execute("INSERT INTO identifiers (book, type, val) VALUES (?,?,?)",
                     (n, "url", f"http://www.gutenberg.org/{9000 + n}"))
    # One book in another category, to prove the filter really filters.
    conn.execute("INSERT INTO books (id, title) VALUES (?,?)",
                 (BIG_COUNT + 1, "A Poem"))
    conn.execute("INSERT INTO books_tags_link (book, tag) VALUES (?,2)",
                 (BIG_COUNT + 1,))
    conn.commit()
    conn.close()
    return str(path)


#: The ten best-known mysteries are the ten HIGHEST ids.
BIG_FAMOUS = list(range(BIG_COUNT, BIG_COUNT - 10, -1))


@pytest.fixture
def big(tmp_path, monkeypatch):
    import json
    pop = {str(9000 + book_id): {"rank": rank}
           for rank, book_id in enumerate(BIG_FAMOUS, start=1)}
    path = tmp_path / "popularity.json"
    path.write_text(json.dumps(pop), encoding="utf-8")
    monkeypatch.setattr(library, "_METADATA_DB",
                        build_big_catalogue(tmp_path / "big.db"))
    monkeypatch.setattr(library, "_APP_DB", build_shelves(tmp_path / "app.db"))
    monkeypatch.setattr(library, "_POPULARITY_PATH", str(path))
    library.forget_last()
    return library


def test_the_best_known_win_even_at_the_highest_ids(big):
    picks = big.recommend(category="mysteries", limit=5)
    assert [b.id for b in picks] == BIG_FAMOUS[:5]


def test_browse_reports_the_true_total_not_the_page(big):
    books, total = big.browse(category="mystery", limit=10)
    assert total == BIG_COUNT
    assert len(books) == 10
    assert [b.id for b in books] == BIG_FAMOUS


def test_a_browse_is_ranked_the_same_way_find_is(big):
    assert [b.id for b in big.find(category="mysteries", limit=3)] == BIG_FAMOUS[:3]


def test_seeded_picks_vary_but_repeat_exactly(big):
    first = [b.id for b in big.recommend(category="mysteries", limit=5, seed=11)]
    assert first == [b.id for b in big.recommend(category="mysteries",
                                                 limit=5, seed=11)]
    assert first != [b.id for b in big.recommend(category="mysteries",
                                                 limit=5, seed=12)]
    # ...and every pick still comes from the top of the ranking.
    top = [b.id for b in big.browse(category="mysteries",
                                    limit=library.SAMPLE_POOL)[0]]
    assert set(first) <= set(top)


def test_without_a_sidecar_it_still_answers(big, tmp_path, monkeypatch):
    monkeypatch.setattr(library, "_POPULARITY_PATH", str(tmp_path / "gone.json"))
    assert library.popularity() == {}
    picks = big.recommend(category="mysteries", limit=5)
    assert len(picks) == 5


def test_the_read_shelf_is_excluded_from_a_big_category(big):
    big.create_shelf("Finished")
    big.add_to_shelf(BIG_FAMOUS[0], "Finished")
    picks = big.recommend(category="mysteries", limit=3)
    assert BIG_FAMOUS[0] not in [b.id for b in picks]
    assert [b.id for b in picks] == BIG_FAMOUS[1:4]


def test_the_safety_cap_is_declared(big):
    assert library.SAFETY_CAP == 250000


# ── the popularity cache ────────────────────────────────────────────────────

def test_popularity_is_parsed_once_and_reparsed_on_change(tmp_path, monkeypatch):
    import json
    path = tmp_path / "popularity.json"
    path.write_text(json.dumps({"84": {"rank": 1}}), encoding="utf-8")
    monkeypatch.setattr(library, "_POPULARITY_PATH", str(path))
    assert library.popularity() == {"84": 1}

    calls = []
    real_open = open

    def counting_open(*args, **kwargs):
        calls.append(args[0])
        return real_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    library.popularity()
    library.popularity()
    assert calls == []                       # cached on (path, mtime)

    monkeypatch.undo()
    monkeypatch.setattr(library, "_POPULARITY_PATH", str(path))
    path.write_text(json.dumps({"84": {"rank": 1}, "2701": {"rank": 2}}),
                    encoding="utf-8")
    import os
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert library.popularity() == {"84": 1, "2701": 2}


# ── LIKE wildcards are the user's data, not SQL syntax ──────────────────────

def test_a_bare_percent_is_not_a_wildcard(lib):
    hits = lib.find("%")
    assert [b.id for b in hits] == [16]          # "50% Off Murder", nothing else


def test_a_bare_underscore_is_not_a_wildcard(lib):
    assert [b.id for b in lib.find("_")] == [17]


def test_a_trailing_percent_does_not_widen_the_search(lib):
    assert lib.find("Dra%") == []                # not a prefix search
    assert [b.id for b in lib.find("Dracula")] == [2]


def test_wildcards_in_an_author_are_literal_too(lib):
    assert lib.find(author="%") == []


# ── shelf names: exact keys only ────────────────────────────────────────────

def test_read_is_not_to_read(lib):
    lib.create_shelf("To Read")
    assert lib.shelf_name("Read") is None
    assert lib.shelf_name("to read") == "To Read"
    assert lib.shelf_name("my to-read shelf") == "To Read"


def test_a_new_name_never_lands_on_an_existing_shelf(lib):
    lib.create_shelf("Shelf")
    made = lib.create_shelf("Locked Shelf Test")
    assert made["action"] == "create_shelf"
    assert sorted(n for n, _c in lib.shelves()) == ["Locked Shelf Test", "Shelf"]


def test_adding_to_an_unknown_shelf_creates_exactly_that_shelf(lib):
    lib.create_shelf("Favorites")
    result = lib.add_to_shelf(2, "Halloween")
    assert result["ok"] and result["shelf"] == "Halloween"
    assert lib.shelf_books("favorites") == []
    assert [b.id for b in lib.shelf_books("Halloween")] == [2]


def test_a_name_with_no_letters_is_refused(lib):
    for junk in ("%", "!!!", "   "):
        result = lib.create_shelf(junk)
        assert result["ok"] is False
        assert "shelf" in result["detail"]
    assert lib.shelves() == []


def test_a_very_long_name_is_trimmed_not_refused(lib):
    result = lib.create_shelf("B" * 200)
    assert result["ok"] is True
    assert len(lib.shelves()[0][0]) == library.MAX_SHELF_NAME


def test_the_same_key_never_makes_a_second_shelf(lib):
    lib.create_shelf("To Read")
    for spelling in ("to read", "TO-READ", "my to read shelf", "to  read"):
        assert lib.create_shelf(spelling)["action"] == "shelf_exists"
    assert len(lib.shelves()) == 1


# ── a locked database answers, it does not hang ─────────────────────────────

def test_a_held_exclusive_lock_fails_fast_and_recovers(lib):
    import threading
    holder_has_lock = threading.Event()
    release = threading.Event()

    def holder():
        conn = sqlite3.connect(lib.app_db_path(), timeout=30,
                               isolation_level=None)
        conn.execute("BEGIN EXCLUSIVE")
        holder_has_lock.set()
        release.wait(30)
        conn.execute("ROLLBACK")
        conn.close()

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert holder_has_lock.wait(10)
    try:
        started = time.monotonic()
        result = lib.add_to_shelf(2, "to read")
        waited = time.monotonic() - started
        assert result["ok"] is False            # honest, not silent
        assert waited < 8, f"waited {waited:.1f}s"
    finally:
        release.set()
        thread.join(10)
    assert lib.add_to_shelf(2, "to read")["ok"] is True


def test_the_write_deadline_and_safety_cap_are_declared():
    assert library.WRITE_DEADLINE == 6.0
    assert library.SAFETY_CAP == 250000
