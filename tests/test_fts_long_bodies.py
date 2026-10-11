"""Long bodies (whole books) are indexed in parts, not truncated.

These drive the same indexing core the ZIM walker uses
(``FTSBuilder._index_records``) with a synthetic 650k-character article, so
the chunking, the article-path stripping and the one-slot-per-article dedup
are all covered without a real pack.
"""

import os
import re
import sqlite3

from puffpai.knowledge.fts_index import (
    CHUNK_CHARS,
    SCHEMA_VERSION,
    FTSBuilder,
    FTSSearcher,
    article_path_for,
    split_body,
)

PART_SEP = '\x00'


# ── Helpers ───────────────────────────────────────────────────────────────

MARKER = "zarquonium"          # occurs only in part 3
COMMON = "ubiquitous"          # occurs in every part


def _filler(n_chars: int, word: str) -> str:
    unit = f"{word} {COMMON} "
    return (unit * (n_chars // len(unit) + 2))[:n_chars].strip()


def _book_body() -> str:
    """~650k characters, with MARKER planted around offset 440k (part 3)."""
    return ' '.join([_filler(440_000, 'alpha'), MARKER,
                     _filler(209_000, 'beta')])


def _make_shard(fts_dir, library_id, records, status='complete'):
    """Write a shard through the real indexing core."""
    os.makedirs(fts_dir, exist_ok=True)
    db_path = os.path.join(fts_dir, f"{library_id}.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        FTSBuilder._ensure_schema(conn)
        count = FTSBuilder._index_records(conn, records)
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [('schema_version', SCHEMA_VERSION),
             ('status', status),
             ('zim_filename', f'{library_id}.zim'),
             ('zim_size', '1234'),
             ('zim_mtime', '5678'),
             ('article_count', str(count))],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT d.path AS path, d.title AS title, f.body AS body "
            "FROM docs d JOIN fts f ON f.rowid = d.id ORDER BY d.id"
        ).fetchall()
    finally:
        conn.close()


def _squash(text: str) -> str:
    return re.sub(r'\s+', '', text)


# ── Chunking ──────────────────────────────────────────────────────────────

def test_long_body_is_split_into_parts_with_full_coverage(tmp_path):
    body = _book_body()
    assert 600_000 < len(body) <= 660_000
    db_path = _make_shard(tmp_path, "books", [("A/Book", "The Book", body)])

    rows = _rows(db_path)
    assert [row['path'] for row in rows] == [
        "A/Book"] + [f"A/Book{PART_SEP}{n}" for n in (2, 3, 4)]
    assert all(row['title'] == "The Book" for row in rows)
    assert all(len(row['body']) <= CHUNK_CHARS for row in rows)

    joined = ''.join(row['body'] for row in rows)
    # Nothing lost, nothing duplicated: only the whitespace at each cut.
    assert _squash(joined) == _squash(body)
    assert len(body) - len(joined) <= len(rows)


def test_split_body_is_pure_for_short_text():
    assert split_body("short body") == ["short body"]
    assert split_body('') == ['']


def test_body_under_the_chunk_size_is_one_bare_row(tmp_path):
    body = _filler(199_000, 'gamma')
    db_path = _make_shard(tmp_path, "short", [("A/Pamphlet", "Pamphlet", body)])

    rows = _rows(db_path)
    assert len(rows) == 1
    assert rows[0]['path'] == "A/Pamphlet"
    assert rows[0]['body'] == body


def test_article_path_for_strips_part_suffixes():
    assert article_path_for("A/Book") == "A/Book"
    assert article_path_for(f"A/Book{PART_SEP}2") == "A/Book"
    assert article_path_for(f"A/Book{PART_SEP}17") == "A/Book"
    # A real ZIM path that merely LOOKS like a part key is untouched.
    assert article_path_for("A/Book#p2") == "A/Book#p2"
    assert article_path_for("A/Book#2") == "A/Book#2"


def test_embedded_nul_paths_round_trip_through_sqlite(tmp_path):
    """The part separator must survive storage, or dedup keys drift."""
    db_path = _make_shard(tmp_path, "nul",
                          [("A/Book", "The Book", _book_body())])
    paths = [row['path'] for row in _rows(db_path)]
    assert paths[1] == f"A/Book{PART_SEP}2"
    assert '\x00' in paths[1] and len(paths[1]) == len("A/Book") + 2


# ── Real paths that collide with the part naming ──────────────────────────

def test_real_hash_p2_path_survives_as_its_own_article(tmp_path):
    """'H/page#p2' is a legal ZIM entry, not part 2 of 'H/page'."""
    _make_shard(tmp_path, "hash", [
        ("H/page#p2", "Real Hash Page", "sausage content unique"),
        ("H/page", "Real Page One", "sausage other content"),
    ])
    hits = FTSSearcher(str(tmp_path)).search("sausage", max_results=10)
    assert sorted((hit['path'], hit['title']) for hit in hits) == [
        ("H/page", "Real Page One"),
        ("H/page#p2", "Real Hash Page"),
    ]


def test_long_book_next_to_a_real_hash_p2_entry_indexes_every_part(tmp_path):
    body = _book_body()
    db_path = _make_shard(tmp_path, "mixed", [
        ("X/page#p2", "Real Hash", "marmalade alpha"),
        ("X/page", "Long Page", body),
    ])
    rows = _rows(db_path)
    # 1 real entry + 4 parts of the book, nothing swallowed by a collision.
    assert len(rows) == 5
    assert sum(len(row['body']) for row in rows[1:]) >= len(body) - len(rows)

    searcher = FTSSearcher(str(tmp_path))
    # The marker lives only in part 3 of the book; both articles survive.
    assert [hit['path'] for hit in searcher.search(MARKER)] == ["X/page"]
    hits = searcher.search("marmalade", max_results=10)
    assert sorted(hit['path'] for hit in hits) == ["X/page#p2"]


# ── Parts must not crowd out other articles ───────────────────────────────

def _multipart_hit_body(term: str, parts: int = 5) -> str:
    """A body of ``parts`` chunks, every one of which contains ``term``."""
    per = CHUNK_CHARS - 1_000
    return " ".join(
        (f"filler{k} " * (per // 9))[:per - 20] + " " + term
        for k in range(parts))


def test_parts_do_not_consume_other_articles_result_slots(tmp_path):
    """Regression: dedup after the per-shard SQL LIMIT emptied the slots."""
    records = [("B/big", "Big Book", _multipart_hit_body("zarquon"))]
    records += [(f"O/{k}", f"Other {k}", f"zarquon other{k} padding text")
                for k in range(4)]
    _make_shard(tmp_path, "crowd", records)
    searcher = FTSSearcher(str(tmp_path))

    for max_results in (3, 5):
        hits = searcher.search("zarquon", max_results=max_results)
        paths = [hit['path'] for hit in hits]
        # Every slot goes to a DIFFERENT article: before the fix the book's
        # five parts filled the shard's fetch and the answer was ['B/big'].
        assert len(paths) == max_results, paths
        assert len(set(paths)) == max_results, paths
        assert paths.count("B/big") <= 1, paths

    # With a slot for all five articles, the book is in there exactly once.
    all_paths = [hit['path'] for hit in
                 searcher.search("zarquon", max_results=5)]
    assert sorted(all_paths) == ["B/big", "O/0", "O/1", "O/2", "O/3"]


def test_parts_do_not_consume_title_slots(tmp_path):
    records = [("B/big", "Manual Big", _multipart_hit_body("zarquon"))]
    records += [(f"O/{k}", f"Manual {k}", "padding text") for k in range(4)]
    _make_shard(tmp_path, "crowd", records)

    hits = FTSSearcher(str(tmp_path)).search_titles("Manual", max_results=10,
                                                    per_library=5)
    paths = [hit['path'] for hit in hits]
    assert len(paths) == 5, paths
    assert paths.count("B/big") == 1, paths


# ── Searching ─────────────────────────────────────────────────────────────

def test_term_only_in_a_later_part_is_found_under_the_article_path(tmp_path):
    _make_shard(tmp_path, "books", [("A/Book", "The Book", _book_body())])

    hits = FTSSearcher(str(tmp_path)).search(MARKER)
    assert len(hits) == 1
    assert hits[0]['path'] == "A/Book"
    assert hits[0]['title'] == "The Book"
    assert MARKER in hits[0]['snippet']


def test_term_in_every_part_returns_the_article_once(tmp_path):
    _make_shard(tmp_path, "books", [("A/Book", "The Book", _book_body())])

    hits = FTSSearcher(str(tmp_path)).search(COMMON)
    assert [hit['path'] for hit in hits] == ["A/Book"]


def test_search_titles_returns_the_article_once(tmp_path):
    _make_shard(tmp_path, "books", [("A/Book", "The Book", _book_body())])

    hits = FTSSearcher(str(tmp_path)).search_titles("Book")
    assert [hit['path'] for hit in hits] == ["A/Book"]


# ── Resume / idempotency ──────────────────────────────────────────────────

def test_reindexing_a_chunked_article_inserts_nothing(tmp_path):
    records = [("A/Book", "The Book", _book_body())]
    db_path = _make_shard(tmp_path, "books", records)
    before = [tuple(row) for row in _rows(db_path)]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert FTSBuilder._index_records(conn, records) == 0
        conn.commit()
    finally:
        conn.close()

    assert [tuple(row) for row in _rows(db_path)] == before
