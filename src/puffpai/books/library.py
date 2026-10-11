"""The household librarian's hands: the REAL Calibre catalogue and the REAL
Calibre-web shelves.

WHY THIS EXISTS. ``media_library.find_books`` answers "do we have any Jane
Austen books" from the catalogue, a title and author lookup and nothing
else. Everything else a librarian does lives here: browse a category, follow a
comic series, recommend something to read next, and keep shelves ("to read",
"favorites") that the household sees in Calibre-web too. This module is that
layer, and it is the ONLY place that touches the two databases.

Two databases, two disciplines:

* **Calibre ``metadata.db``** — READ ONLY, always opened ``mode=ro`` (the same
  pattern ``media_bridge.search_books`` uses). Curation writes it from a
  separate offline script; we never do.
* **Calibre-web ``app.db``** — read/write, because a shelf IS a Calibre-web
  shelf. Calibre-web is running while we write, so every write is one short
  transaction on a connection with a busy timeout. Rows are inserted with the
  exact column set and timestamp format SQLAlchemy writes, so the web UI cannot
  tell our shelf from one made by hand.

The cardinal rule of :mod:`puffpai.media_library` holds here: **never answer
from the model.** Every title, author, shelf and count returned by this module
came out of a real row, and a write that did not happen is reported as not
having happened.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from puffpai.books.categories import (CATEGORIES, SUBJECTS_COLUMN_LABEL,
                                      UMBRELLA_TAGS)
from puffpai.media_library import normalize_title, title_similarity

logger = logging.getLogger(__name__)

#: Test/ops override hooks (same shape as ``dashboard_display._STATE_PATH``).
_METADATA_DB: str | None = None
_APP_DB: str | None = None
_POPULARITY_PATH: str | None = None

#: Which format the reader should open when a book has several. EPUB reflows,
#: the comic formats are the only thing a CBR/CBZ book HAS, PDF is the last
#: resort. MOBI is deliberately absent: the web reader cannot render it, so a
#: MOBI-only book opens at its detail page instead.
FORMAT_PREFERENCE = ("EPUB", "CBZ", "CBR", "PDF")

#: Shelves that mean "already read" — excluded from recommendations when they
#: exist. Matched loosely, like every other shelf name.
READ_SHELF_NAMES = ("read", "finished", "already read")

#: How long "it"/"that one" still means the books just listed.
LAST_TTL = 600.0

#: How many rows a browse/recommend pool may pull before ranking. A category
#: browse over a 60k-row library must not read the whole table; the cap is
#: declared here rather than hidden in a query.
POOL_LIMIT = 800

#: Hard safety cap on a phase-1 id sweep. A curated category holds a few
#: thousand books and the whole catalogue ~60k; the cap is the guard rail for
#: a library an order of magnitude larger than this one, not a page size.
SAFETY_CAP = 250000

#: When a SEED is given, the pick is sampled from this many top-ranked books
#: instead of always being the same few — deterministic variety.
SAMPLE_POOL = 40

#: How wide an unfiltered "what should I read next" casts when the household
#: keeps no shelves yet: the globally best-known books, nothing else.
GLOBAL_TOP = 200

#: A confident title hit, same threshold the movie matcher uses.
TITLE_MATCH_THRESHOLD = 0.86

_CATEGORY_SET = {c.casefold(): c for c in CATEGORIES}
_UMBRELLA_SET = {t.casefold() for t in UMBRELLA_TAGS}
_GUTENBERG_RE = re.compile(r"gutenberg\.org/(?:ebooks/)?(\d+)", re.IGNORECASE)


# ── paths ───────────────────────────────────────────────────────────────────

def _cfg_media():
    try:
        from puffpai.config import get_config
        return get_config().media_server
    except Exception as exc:                       # pragma: no cover - guard
        logger.debug("book config unavailable: %s", exc)
        return None


def metadata_db_path() -> str:
    if _METADATA_DB:
        return _METADATA_DB
    ms = _cfg_media()
    return str(getattr(ms, "calibre_db_path", "") or "") if ms else ""


def app_db_path() -> str:
    if _APP_DB:
        return _APP_DB
    ms = _cfg_media()
    return str(getattr(ms, "calibre_web_db_path", "") or "") if ms else ""


def popularity_path() -> str:
    if _POPULARITY_PATH:
        return _POPULARITY_PATH
    ms = _cfg_media()
    return str(getattr(ms, "books_popularity_path", "") or "") if ms else ""


def catalogue_available() -> bool:
    """True when the real Calibre catalogue can be opened."""
    path = metadata_db_path()
    return bool(path) and os.path.isfile(path)


def shelves_available() -> bool:
    """True when Calibre-web's own database can be opened."""
    path = app_db_path()
    return bool(path) and os.path.isfile(path)


@contextmanager
def _connect_ro(path: str):
    """Read-only, always closed. Calibre's own database is never written here."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


#: The whole of ONE shelf write, start to finish. ``busy_timeout`` is per
#: STATEMENT, so an operation made of six statements could wait six times over
#: — a live lock-out measured 14.4s behind a held EXCLUSIVE. Taking the write
#: lock up front with a single ``BEGIN IMMEDIATE`` makes that one statement the
#: only one that can ever wait, so this really is the deadline.
WRITE_DEADLINE = 6.0


def _begin_immediate(conn) -> None:
    """Take the write lock within :data:`WRITE_DEADLINE`, or raise.

    SQLite's own ``busy_timeout`` is honoured per STATEMENT and its retry
    ladder overshoots (6,000 ms measured as 8.8 s on Windows), so the deadline
    is kept here against a monotonic clock: fail immediately, sleep, retry,
    give up on time. Once this returns we hold the write lock and no later
    statement in the operation can block on another writer.
    """
    conn.execute("PRAGMA busy_timeout=0")
    deadline = time.monotonic() + WRITE_DEADLINE
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    # Held the lock: only a lingering READER could now delay the COMMIT, and a
    # second is plenty for that.
    conn.execute("PRAGMA busy_timeout=1000")


@contextmanager
def _connect_rw(path: str):
    """Calibre-web's database, which Calibre-web itself has open.

    Opens the write transaction IMMEDIATELY: either we hold the lock within
    :data:`WRITE_DEADLINE` or we raise ``sqlite3.OperationalError`` and the
    caller reports an honest "couldn't do it" — never a 14-second silence.
    Commits on a clean exit, rolls back on any exception.
    """
    conn = sqlite3.connect(path, timeout=WRITE_DEADLINE, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        _begin_immediate(conn)
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:            # pragma: no cover - already gone
                pass
            raise
    finally:
        conn.close()


def _now_stamp() -> str:
    """The timestamp format Calibre-web's own rows carry."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


# ── the book ────────────────────────────────────────────────────────────────

@dataclass
class Book:
    id: int
    title: str
    authors: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    categories: list = field(default_factory=list)
    series: str = ""
    series_index: float | None = None
    formats: list = field(default_factory=list)
    gutenberg_id: str = ""
    is_comic: bool = False
    is_mine: bool = False

    @property
    def author(self) -> str:
        return ", ".join(self.authors)

    @property
    def label(self) -> str:
        """"Dracula by Bram Stoker" — how one book is spoken."""
        if self.series and self.series_index:
            idx = int(self.series_index) if float(self.series_index).is_integer() \
                else self.series_index
            head = f"{self.title} ({self.series} #{idx})"
        else:
            head = self.title
        return f"{head} by {self.author}" if self.authors else head

    @property
    def best_format(self) -> str:
        """The format the reader should open, or "" (MOBI-only → detail page)."""
        have = {f.upper() for f in self.formats}
        for fmt in FORMAT_PREFERENCE:
            if fmt in have:
                return fmt
        return ""


def _split(value) -> list:
    return [p.strip() for p in str(value or "").split("|") if p.strip()]


def _row_to_book(row) -> Book:
    tags = _split(row["tags"])
    cats = [_CATEGORY_SET[t.casefold()] for t in tags if t.casefold() in _CATEGORY_SET]
    formats = sorted({f.upper() for f in _split(row["formats"])})
    gid = ""
    m = _GUTENBERG_RE.search(str(row["gutenberg_url"] or ""))
    if m:
        gid = m.group(1)
    series = str(row["series"] or "")
    lower_tags = {t.casefold() for t in tags}
    is_comic = ("comics" in lower_tags
                or bool({"CBR", "CBZ"} & set(formats)))
    try:
        idx = float(row["series_index"]) if series else None
    except (TypeError, ValueError):
        idx = None
    return Book(
        id=int(row["id"]), title=str(row["title"] or ""),
        authors=_split(row["authors"]), tags=tags, categories=cats,
        series=series, series_index=idx, formats=formats,
        gutenberg_id=gid, is_comic=is_comic,
        is_mine="my library" in lower_tags,
    )


_SELECT = """
SELECT b.id, b.title,
       (SELECT GROUP_CONCAT(a.name, '|') FROM books_authors_link bal
          JOIN authors a ON a.id = bal.author WHERE bal.book = b.id) AS authors,
       (SELECT GROUP_CONCAT(t.name, '|') FROM books_tags_link btl
          JOIN tags t ON t.id = btl.tag WHERE btl.book = b.id) AS tags,
       (SELECT s.name FROM books_series_link bsl
          JOIN series s ON s.id = bsl.series WHERE bsl.book = b.id LIMIT 1) AS series,
       b.series_index AS series_index,
       (SELECT GROUP_CONCAT(d.format, '|') FROM data d WHERE d.book = b.id) AS formats,
       (SELECT i.val FROM identifiers i WHERE i.book = b.id AND i.type = 'url'
          AND i.val LIKE '%gutenberg.org%' LIMIT 1) AS gutenberg_url
FROM books b
"""


# ── categories, spoken loosely ──────────────────────────────────────────────

def _norm(text) -> str:
    low = str(text or "").lower().replace("&", " and ")
    low = re.sub(r"[^a-z0-9 ]+", " ", low)
    return " ".join(low.split())


#: Spoken forms → the controlled vocabulary. The household says "sci fi" and
#: "kids books"; the tag says "Science Fiction" and "Children's & Young
#: Readers". This table is the only place that difference lives.
_ALIAS_SEED = {
    "General Fiction": ("fiction", "novels", "general"),
    "Science Fiction": ("sci fi", "scifi", "sf", "space", "science fiction books"),
    "Fantasy & Folklore": ("fantasy", "myth", "mythology", "folklore",
                           "fairy tales", "legends"),
    "Mystery & Detective": ("mystery", "mysteries", "detective", "detectives",
                            "crime", "whodunit", "thriller", "thrillers"),
    "Horror & Gothic": ("horror", "gothic", "scary", "spooky"),
    "Adventure & Westerns": ("adventure", "adventures", "western", "westerns",
                             "sea stories", "sea story", "pirates"),
    "Romance": ("romance", "romances", "love stories"),
    "Humor & Satire": ("humor", "humour", "satire", "funny", "comedy"),
    "Short Stories": ("short stories", "short story"),
    "Children's & Young Readers": ("kids", "kid", "children", "childrens",
                                   "young readers", "young adult", "ya",
                                   "picture books", "bedtime stories"),
    "Poetry": ("poetry", "poems", "verse"),
    "Drama & Plays": ("drama", "plays", "theatre", "theater", "shakespeare plays"),
    "History": ("history", "historical"),
    "War & Military": ("war", "military", "wwii", "world war"),
    "Biography & Letters": ("biography", "biographies", "memoir", "memoirs",
                            "letters", "autobiography"),
    "Travel & Exploration": ("travel", "exploration", "explorers", "expeditions"),
    "Philosophy": ("philosophy", "philosophers"),
    "Religion & Spirituality": ("religion", "religious", "spirituality",
                                "spiritual", "bible"),
    "Science & Nature": ("science", "nature", "biology", "physics", "astronomy",
                         "birds", "plants"),
    "Technology & Engineering": ("technology", "tech", "engineering",
                                 "electronics", "mechanics"),
    "Medicine & Health": ("medicine", "medical", "health", "first aid",
                          "anatomy", "nursing"),
    "Homestead & Cooking": ("homestead", "homesteading", "cooking", "cookbook",
                            "cookbooks", "recipes", "garden", "gardening",
                            "farm", "farming", "canning", "livestock"),
    "Survival & Preparedness": ("survival", "survivalist", "prepping", "prepper",
                                "preparedness", "emergency", "bushcraft"),
    "Art & Music": ("art", "music", "architecture", "painting",
                    "photography"),
    "Language & Reference": ("language", "languages", "reference", "dictionary",
                             "dictionaries", "grammar", "encyclopedia"),
    "Society & Politics": ("society", "politics", "political", "law", "legal",
                           "economics", "economy", "money", "government"),
    "Magazines & Periodicals": ("magazines", "magazine", "periodicals", "issues"),
    "Comics": ("comics", "comic", "comic books", "graphic novel",
               "graphic novels", "manga", "funny papers"),
}


def _build_aliases() -> dict:
    out: dict = {}
    for cat in CATEGORIES:
        out.setdefault(_norm(cat), cat)
        # "Homestead & Cooking" also answers to each of its parts.
        for part in re.split(r"[:,]|\band\b", _norm(cat)):
            part = part.strip()
            if len(part) >= 4:
                out.setdefault(part, cat)
    for cat, aliases in _ALIAS_SEED.items():
        for alias in aliases:
            out.setdefault(_norm(alias), cat)
    return out


_CATEGORY_ALIASES = _build_aliases()
#: Longest first, so "science fiction" is never shadowed by "science".
_ALIAS_PHRASES = tuple(sorted(_CATEGORY_ALIASES, key=len, reverse=True))

_CATEGORY_TAIL_RE = re.compile(
    r"\b(books?|novels?|stories|section|shelf|category|categories|genre|"
    r"ones?|stuff|titles?)\b")


def resolve_category(text) -> str | None:
    """Map a spoken category ("sci fi", "kids books") onto CATEGORIES, or None."""
    n = _norm(text)
    if not n:
        return None
    if n in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[n]
    stripped = " ".join(_CATEGORY_TAIL_RE.sub(" ", n).split())
    if stripped in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[stripped]
    for phrase in _ALIAS_PHRASES:
        if re.search(r"\b" + re.escape(phrase) + r"\b", n):
            return _CATEGORY_ALIASES[phrase]
    return None


# ── reading the catalogue ───────────────────────────────────────────────────

def _subjects_column(conn) -> int | None:
    """The id of the ``subjects`` custom column, or None when curation has not
    created it yet — in which case subject search is silently unavailable."""
    try:
        row = conn.execute(
            "SELECT id FROM custom_columns WHERE label = ?",
            (SUBJECTS_COLUMN_LABEL,)).fetchone()
    except sqlite3.Error:
        return None
    return int(row["id"]) if row else None


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (name,)).fetchone()
    return row is not None


def _like(value) -> str:
    """A user's words as a LIKE pattern: their own ``%``/``_``/``\\`` are
    literal. Without this, ``find("%")`` matches the whole catalogue."""
    text = str(value or "")
    for ch in ("\\", "%", "_"):
        text = text.replace(ch, "\\" + ch)
    return f"%{text}%"


def _tag_clause(name: str, params: list) -> str:
    params.append(name)
    return ("EXISTS (SELECT 1 FROM books_tags_link btl JOIN tags t "
            "ON t.id = btl.tag WHERE btl.book = b.id AND t.name = ? COLLATE NOCASE)")


def _conditions(conn, *, query="", author="", category="", subject="",
                series="", tag="", book_id=None, mine_only=False,
                comics_only=False):
    """The shared WHERE for both phases: ``(clauses, params, bail)``. ``bail``
    is True when the filter can never match — a subject search with no
    ``subjects`` custom column, which is silently unavailable by design."""
    where: list = []
    params: list = []
    if book_id is not None:
        where.append("b.id = ?")
        params.append(int(book_id))
    if query:
        where.append("b.title LIKE ? ESCAPE '\\'")
        params.append(_like(query))
    if author:
        where.append("EXISTS (SELECT 1 FROM books_authors_link bal JOIN authors a "
                     "ON a.id = bal.author WHERE bal.book = b.id "
                     "AND a.name LIKE ? ESCAPE '\\')")
        params.append(_like(author))
    if series:
        where.append("EXISTS (SELECT 1 FROM books_series_link bsl JOIN series s "
                     "ON s.id = bsl.series WHERE bsl.book = b.id "
                     "AND s.name LIKE ? ESCAPE '\\')")
        params.append(_like(series))
    if category:
        where.append(_tag_clause(category, params))
    if tag:
        where.append(_tag_clause(tag, params))
    if mine_only:
        where.append(_tag_clause("My Library", params))
    if comics_only:
        where.append(_tag_clause("Comics", params))
    if subject:
        col = _subjects_column(conn)
        if col is None:
            return [], [], True
        link = f"books_custom_column_{col}_link"
        table = f"custom_column_{col}"
        if _table_exists(conn, link):
            where.append(
                f"EXISTS (SELECT 1 FROM {link} l JOIN {table} c ON c.id = l.value "
                f"WHERE l.book = b.id AND c.value LIKE ? ESCAPE '\\')")
        elif _table_exists(conn, table):
            where.append(
                f"EXISTS (SELECT 1 FROM {table} c WHERE c.book = b.id "
                f"AND c.value LIKE ? ESCAPE '\\')")
        else:
            return [], [], True
        params.append(_like(subject))
    return where, params, False


def _rows(conn, *, pool=POOL_LIMIT, **filters):
    where, params, bail = _conditions(conn, **filters)
    if bail:
        return []
    sql = _SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY b.id LIMIT ?"
    params = list(params) + [int(pool)]
    return list(conn.execute(sql, params).fetchall())


# ── two-phase ranking ───────────────────────────────────────────────────────
#
# WHY TWO PHASES. After curation a category holds thousands of books (General
# Fiction ~12k, Mystery ~2k). Ranking a CAPPED slice of them by popularity
# ranks the lowest Calibre IDS, not the best-known books — "recommend a
# mystery" would answer with whatever was imported first instead of The Hound
# of the Baskervilles. So phase 1 sweeps EVERY id that matches the filter with
# a light two-column query (id + the Gutenberg identifier), phase 2 ranks those
# ids in Python against the popularity sidecar, and only the handful that win
# are loaded as full Book rows.

_LIGHT_SELECT = """
SELECT b.id AS id,
       (SELECT i.val FROM identifiers i WHERE i.book = b.id AND i.type = 'url'
          AND i.val LIKE '%gutenberg.org%' LIMIT 1) AS gutenberg_url
FROM books b
"""


def _light_pairs(conn, **filters) -> list:
    """``[(book id, gutenberg id or "")]`` for EVERY book matching the filter."""
    where, params, bail = _conditions(conn, **filters)
    if bail:
        return []
    sql = _LIGHT_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY b.id LIMIT ?"
    params = list(params) + [SAFETY_CAP]
    out = []
    for row in conn.execute(sql, params):
        m = _GUTENBERG_RE.search(str(row["gutenberg_url"] or ""))
        out.append((int(row["id"]), m.group(1) if m else ""))
    return out


def _rank_ids(pairs, *, limit: int, seed=None, exclude=None,
              personal_first=False) -> list:
    """Phase 2: the ids worth reading, best-known first.

    Popularity rank ascending; a book the sidecar does not know comes after
    every book it does, ordered by id so the answer is stable rather than
    arbitrary. ``seed`` samples from the top :data:`SAMPLE_POOL` instead of
    always returning the same few — deterministic variety, not a new order.
    """
    ranks = popularity()
    drop = set(exclude or ())
    scored = []
    for book_id, gid in pairs:
        if book_id in drop:
            continue
        rank = ranks.get(gid, _NO_RANK) if gid else _NO_RANK
        personal = 0 if (personal_first and not gid) else 1
        scored.append((personal, rank, book_id))
    scored.sort()
    want = max(int(limit), 1)
    if seed is not None and len(scored) > want:
        top = scored[:max(SAMPLE_POOL, want)]
        picked = random.Random(seed).sample(top, want)
        picked.sort()
        return [book_id for _p, _r, book_id in picked]
    return [book_id for _p, _r, book_id in scored[:want]]


def get_many(ids) -> list:
    """Full Book rows for a handful of ids, in the order asked for."""
    wanted = [int(i) for i in (ids or [])]
    if not wanted or not catalogue_available():
        return []
    try:
        with _connect_ro(metadata_db_path()) as conn:
            marks = ",".join("?" * len(wanted))
            rows = conn.execute(_SELECT + f" WHERE b.id IN ({marks})",
                                wanted).fetchall()
    except sqlite3.Error as exc:
        logger.warning("calibre catalogue read failed: %s", exc)
        return []
    by_id = {int(r["id"]): _row_to_book(r) for r in rows}
    return [by_id[i] for i in wanted if i in by_id]


def browse(*, category: str = "", author: str = "", series: str = "",
           tag: str = "", mine_only: bool = False, comics_only: bool = False,
           limit: int = 10, seed=None, exclude=None) -> tuple:
    """``(best-known books first, TRUE total)`` for a filter with no title.

    The total is the real number of matching books, not the number returned —
    "502 in Survival & Preparedness; the best known are …" is only honest if
    the 502 was counted rather than the page size.
    """
    if not catalogue_available():
        return [], 0
    # ``resolve_category`` only ever yields a name from CATEGORIES, so the tag
    # we match on is always one of OUR categories — never a raw Library of
    # Congress subject tag that happens to read like one.
    cat = resolve_category(category) if category else ""
    if category and not cat:
        return [], 0
    try:
        with _connect_ro(metadata_db_path()) as conn:
            pairs = _light_pairs(conn, category=cat, author=author,
                                 series=series, tag=tag, mine_only=mine_only,
                                 comics_only=comics_only)
    except sqlite3.Error as exc:
        logger.warning("calibre catalogue sweep failed: %s", exc)
        return [], 0
    drop = set(exclude or ())
    total = len([pair for pair in pairs if pair[0] not in drop])
    ids = _rank_ids(pairs, limit=limit, seed=seed, exclude=drop,
                    personal_first=mine_only)
    return get_many(ids), total


def _score(query: str, book: Book) -> tuple:
    """exact > prefix > contains > fuzzy, tolerant of articles and punctuation."""
    nq, nt = normalize_title(query), normalize_title(book.title)
    sim = title_similarity(query, book.title)
    if nq and nt == nq:
        tier = 4
    elif nq and nt.startswith(nq):
        tier = 3
    elif nq and nq in nt:
        tier = 2
    else:
        tier = 1
    return (tier, sim, -len(nt))


def find(query: str = "", *, author: str = "", category: str = "",
         subject: str = "", series: str = "", tag: str = "",
         mine_only: bool = False, comics_only: bool = False,
         limit: int = 10) -> list:
    """Search the REAL catalogue. Returns ranked :class:`Book` rows (possibly
    empty); an unreachable catalogue also returns [] — callers check
    :func:`catalogue_available` when they need to tell the two apart."""
    if not catalogue_available():
        return []
    cat = resolve_category(category) if category else ""
    if category and not cat:
        return []
    q = (query or "").strip().strip("?.!,")
    if not q and not subject:
        # Nothing to rank a title against: this is a BROWSE, so the best-known
        # books come first instead of the lowest Calibre ids.
        books, _total = browse(category=cat, author=author, series=series,
                               tag=tag, mine_only=mine_only,
                               comics_only=comics_only, limit=limit)
        return books
    try:
        with _connect_ro(metadata_db_path()) as conn:
            rows = _rows(conn, query=q, author=author, category=cat,
                         subject=subject, series=series, tag=tag,
                         mine_only=mine_only, comics_only=comics_only,
                         pool=max(int(limit) * 20, POOL_LIMIT))
            if q and not rows and not re.search(r"[%_]", q):
                # "the hobbit" against a row titled "Hobbit, The": try the
                # words rather than the phrase before giving up.
                words = [w for w in normalize_title(q).split() if len(w) > 2]
                if words:
                    clause = " AND ".join(
                        ["b.title LIKE ? ESCAPE '\\'"] * len(words))
                    sql = _SELECT + " WHERE " + clause + " ORDER BY b.id LIMIT ?"
                    rows = list(conn.execute(
                        sql, [_like(w) for w in words] + [POOL_LIMIT]).fetchall())
    except sqlite3.Error as exc:
        logger.warning("calibre catalogue read failed: %s", exc)
        return []
    books = [_row_to_book(r) for r in rows]
    if q:
        books.sort(key=lambda b: _score(q, b), reverse=True)
    else:
        books.sort(key=lambda b: (normalize_title(b.series or b.title),
                                  b.series_index or 0, b.id))
    return books[:max(int(limit), 1)]


def _find_browse(**kwargs):
    """find() with no title = a browse: best-known first, with the real total."""
    return browse(**kwargs)


def get(book_id) -> Book | None:
    """One book by its Calibre id, or None."""
    if not catalogue_available():
        return None
    try:
        with _connect_ro(metadata_db_path()) as conn:
            rows = _rows(conn, book_id=int(book_id), pool=1)
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.warning("calibre book read failed: %s", exc)
        return None
    return _row_to_book(rows[0]) if rows else None


def best_match(query: str) -> Book | None:
    """The one book a spoken title names, or None when nothing is close enough.

    A confident fuzzy hit (the movie matcher's threshold) OR a whole-phrase
    containment — "walking dead" names "The Walking Dead #54" even though the
    strings are only two-thirds similar.
    """
    books = find(query, limit=10)
    if not books:
        return None
    nq = normalize_title(query)
    for book in books:
        if title_similarity(query, book.title) >= TITLE_MATCH_THRESHOLD:
            return book
    if len(nq) >= 5:
        for book in books:
            if nq in normalize_title(book.title):
                return book
    return None


def nearest_titles(query: str, limit: int = 3) -> list:
    """The closest things we ACTUALLY have — the honest miss's companions."""
    books = find(query, limit=max(limit * 3, 9))
    return books[:limit]


# ── popularity (offline sidecar) ────────────────────────────────────────────

_pop_cache: dict = {"path": None, "mtime": None, "data": {}}
_pop_lock = threading.Lock()


def popularity() -> dict:
    """``{gutenberg_id: rank}`` from the offline sidecar; {} when absent."""
    path = popularity_path()
    with _pop_lock:
        try:
            mtime = os.path.getmtime(path) if path else None
        except OSError:
            mtime = None
        if mtime is None:
            _pop_cache.update(path=path, mtime=None, data={})
            return {}
        if _pop_cache["path"] == path and _pop_cache["mtime"] == mtime:
            return _pop_cache["data"]
        data: dict = {}
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            for gid, entry in (raw or {}).items():
                if isinstance(entry, dict) and entry.get("rank") is not None:
                    data[str(gid)] = int(entry["rank"])
                elif isinstance(entry, (int, float)):
                    data[str(gid)] = int(entry)
        except (OSError, ValueError, TypeError) as exc:
            logger.info("book popularity sidecar unreadable: %s", exc)
            data = {}
        _pop_cache.update(path=path, mtime=mtime, data=data)
        return data


_NO_RANK = 10 ** 9


def _shelf_categories() -> list:
    """The categories the household's OWN shelves are made of — what an
    unfiltered "what should I read next" should draw on before the world's."""
    cats: list = []
    for name, count in shelves():
        if not count or _shelf_key(name) in {_shelf_key(n) for n in READ_SHELF_NAMES}:
            continue
        for book in shelf_books(name):
            for cat in book.categories:
                if cat not in cats:
                    cats.append(cat)
    return cats


def recommend(*, category: str = "", author: str = "", like_book_id=None,
              shelf: str = "", limit: int = 5, mine_only: bool = False,
              seed=None) -> list:
    """Books worth reading next, out of the real catalogue and nothing else.

    Two phases (see the ranking section above): every id matching the filter is
    swept, then the popularity sidecar picks the best-known of them — so
    "recommend a mystery" answers with the mysteries the world actually reads,
    not the ones that happen to hold the lowest Calibre ids. Books on a
    "Read"/"Finished" shelf are left out when such a shelf exists.
    ``like_book_id`` narrows to that book's category and puts its author first;
    ``seed`` gives deterministic variety inside the top :data:`SAMPLE_POOL`.
    """
    like = get(like_book_id) if like_book_id else None
    if like is not None and not category:
        category = like.categories[0] if like.categories else ""
    exclude = set(_read_shelf_ids())
    if like is not None:
        exclude.add(like.id)

    if shelf:
        pairs = [(b.id, b.gutenberg_id) for b in shelf_books(shelf)]
        ids = _rank_ids(pairs, limit=limit, seed=seed, exclude=exclude)
        return get_many(ids)

    unfiltered = not (category or author or mine_only)
    if unfiltered:
        # No filter at all: start from what the household's own shelves are
        # made of; with no shelves yet, the globally best-known books.
        for cat in _shelf_categories():
            books, _total = browse(category=cat, limit=limit, seed=seed,
                                   exclude=exclude)
            if books:
                return books
        books, _total = browse(limit=min(limit, GLOBAL_TOP), seed=seed,
                               exclude=exclude)
        return books

    books, _total = browse(category=category, author=author,
                           mine_only=mine_only, limit=limit, seed=seed,
                           exclude=exclude)
    if not books and like is not None:
        # The only book in that category WAS the one they liked: widen to the
        # whole catalogue rather than answer "nothing".
        books, _total = browse(limit=limit, seed=seed, exclude=exclude)
    if like is not None and like.authors:
        # Same author first, popularity order preserved within each group.
        wanted = {a.casefold() for a in like.authors}
        books.sort(key=lambda b: 0 if {a.casefold() for a in b.authors} & wanted
                   else 1)
    return books


# ── shelves (Calibre-web's own) ─────────────────────────────────────────────

def _result(ok: bool, action: str, book=None, shelf: str = "",
            detail: str = "") -> dict:
    return {"ok": bool(ok), "action": action,
            "book": book.title if isinstance(book, Book) else (book or ""),
            "shelf": shelf, "detail": detail}


_SHELF_UNAVAILABLE = "I can't reach your Calibre-web shelves right now."


#: Cap on a shelf name. Longer than this is a sentence, not a shelf.
MAX_SHELF_NAME = 60

_SHELF_LEAD_RE = re.compile(r"^(?:my|the|our)\s+", re.IGNORECASE)
_SHELF_TAIL_RE = re.compile(r"\s+(?:shelf|shelves|list|pile)$", re.IGNORECASE)


def _shelf_key(name) -> str:
    """The ONE key two spellings of the same shelf share.

    Casefolded, punctuation dropped, a leading "my/the/our" and a trailing
    "shelf/list/pile" removed: "To Read", "to-read", "my to read shelf" are one
    shelf. Nothing looser: a SUBSTRING pass would send "Read" to "To Read"
    and a brand-new "Locked Shelf Test" to an existing shelf called "Shelf",
    and a shelf write would land somewhere nobody asked for.
    """
    text = " ".join(str(name or "").split())
    text = _SHELF_LEAD_RE.sub("", text)
    text = _SHELF_TAIL_RE.sub("", text)
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _find_shelf_row(conn, name):
    """The shelf with EXACTLY this key, or None. No substring, no fuzzy."""
    key = _shelf_key(name)
    if not key:
        return None
    for row in conn.execute("SELECT id, name FROM shelf").fetchall():
        if _shelf_key(row["name"]) == key:
            return row
    return None


def _clean_shelf_name(name) -> tuple:
    """``(name, error)`` — a name we are willing to create, or why not."""
    clean = " ".join(str(name or "").split())
    if not _shelf_key(clean):
        return "", "that isn't a name I can put on a shelf"
    if len(clean) > MAX_SHELF_NAME:
        clean = clean[:MAX_SHELF_NAME].rstrip()
    # A spoken name arrives as "to-read" / "halloween picks"; on the Calibre-web
    # sidebar it should read like a shelf label. Only an all-lowercase name is
    # dressed up — anything the owner typed with her own capitals is left alone.
    if clean == clean.lower():
        clean = " ".join(w.capitalize() for w in clean.replace("-", " ").split())
    return clean, ""


def _owner_user_id(conn) -> int | None:
    """The first admin-role user — the shelf's owner, so it shows up in the web
    UI as the household's shelf and not an orphan. Calibre-web's ROLE_ADMIN is
    bit 0; 'Guest' is never it."""
    for row in conn.execute("SELECT id, name, role FROM user ORDER BY id").fetchall():
        if str(row["name"]).strip().lower() == "guest":
            continue
        try:
            role = int(row["role"] or 0)
        except (TypeError, ValueError):
            role = 0
        if role & 1:
            return int(row["id"])
    row = conn.execute(
        "SELECT id FROM user WHERE name <> 'Guest' ORDER BY id LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def shelves() -> list:
    """``[(name, book count), …]`` from Calibre-web, or [] when unreachable."""
    if not shelves_available():
        return []
    try:
        with _connect_ro(app_db_path()) as conn:
            rows = conn.execute(
                "SELECT s.name AS name, COUNT(l.id) AS n FROM shelf s "
                "LEFT JOIN book_shelf_link l ON l.shelf = s.id "
                "GROUP BY s.id ORDER BY s.name COLLATE NOCASE").fetchall()
        return [(str(r["name"] or ""), int(r["n"] or 0)) for r in rows]
    except sqlite3.Error as exc:
        logger.warning("calibre-web shelf read failed: %s", exc)
        return []


def shelf_name(name) -> str | None:
    """The REAL shelf name a spoken one refers to, or None."""
    if not shelves_available():
        return None
    try:
        with _connect_ro(app_db_path()) as conn:
            row = _find_shelf_row(conn, name)
    except sqlite3.Error:
        return None
    return str(row["name"]) if row else None


def shelf_book_ids(name) -> list:
    if not shelves_available():
        return []
    try:
        with _connect_ro(app_db_path()) as conn:
            row = _find_shelf_row(conn, name)
            if row is None:
                return []
            return [int(r["book_id"]) for r in conn.execute(
                'SELECT book_id FROM book_shelf_link WHERE shelf = ? '
                'ORDER BY "order", id', (row["id"],)).fetchall()]
    except sqlite3.Error as exc:
        logger.warning("calibre-web shelf read failed: %s", exc)
        return []


def shelf_books(name) -> list:
    """The real books on a shelf, in shelf order. Ids Calibre no longer has are
    dropped rather than invented."""
    ids = shelf_book_ids(name)
    out = []
    for book_id in ids:
        book = get(book_id)
        if book is not None:
            out.append(book)
    return out


def _read_shelf_ids() -> set:
    """Book ids on a "Read"/"Finished" shelf, when the household keeps one."""
    out: set = set()
    for name, _count in shelves():
        if _shelf_key(name) in {_shelf_key(n) for n in READ_SHELF_NAMES}:
            out.update(shelf_book_ids(name))
    return out


def _insert_shelf(conn, clean: str) -> None:
    owner = _owner_user_id(conn)
    stamp = _now_stamp()
    conn.execute(
        "INSERT INTO shelf (uuid, name, is_public, user_id, "
        "kobo_sync, created, last_modified) VALUES (?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), clean, 1, owner, 0, stamp, stamp))


def create_shelf(name: str) -> dict:
    """Make a Calibre-web shelf (public, owned by the first admin user)."""
    clean, error = _clean_shelf_name(name)
    if error:
        return _result(False, "create_shelf", shelf="", detail=error)
    if not shelves_available():
        return _result(False, "create_shelf", shelf=clean, detail=_SHELF_UNAVAILABLE)
    try:
        with _connect_rw(app_db_path()) as conn:
            existing = _find_shelf_row(conn, clean)
            if existing is not None:
                return _result(True, "shelf_exists", shelf=str(existing["name"]),
                               detail="that shelf already exists")
            _insert_shelf(conn, clean)
        return _result(True, "create_shelf", shelf=clean)
    except sqlite3.Error as exc:
        logger.warning("shelf create failed: %s", exc)
        return _result(False, "create_shelf", shelf=clean, detail=str(exc))


def add_to_shelf(book_id, shelf: str, create: bool = True) -> dict:
    """Put one real book on one real shelf."""
    book = get(book_id)
    if book is None:
        return _result(False, "add", shelf=shelf,
                       detail="that book isn't in the catalogue")
    if not shelves_available():
        return _result(False, "add", book=book, shelf=shelf, detail=_SHELF_UNAVAILABLE)
    try:
        with _connect_rw(app_db_path()) as conn:
            row = _find_shelf_row(conn, shelf)
            if row is None:
                if not create:
                    return _result(False, "add", book=book, shelf=shelf,
                                   detail="there's no shelf by that name")
                clean, error = _clean_shelf_name(shelf)
                if error:
                    return _result(False, "add", book=book, shelf=shelf,
                                   detail=error)
                _insert_shelf(conn, clean)     # same connection, same lock
                row = _find_shelf_row(conn, clean)
                if row is None:                    # pragma: no cover - guard
                    return _result(False, "add", book=book, shelf=shelf,
                                   detail="couldn't create that shelf")
            name = str(row["name"])
            dup = conn.execute(
                "SELECT 1 FROM book_shelf_link WHERE shelf = ? AND book_id = ?",
                (row["id"], book.id)).fetchone()
            if dup:
                return _result(True, "already_on_shelf", book=book, shelf=name)
            nxt = conn.execute(
                'SELECT COALESCE(MAX("order"), 0) + 1 AS n FROM book_shelf_link '
                'WHERE shelf = ?', (row["id"],)).fetchone()["n"]
            conn.execute(
                    'INSERT INTO book_shelf_link (book_id, "order", shelf, '
                    'date_added) VALUES (?,?,?,?)',
                    (book.id, int(nxt or 1), int(row["id"]), _now_stamp()))
            return _result(True, "add", book=book, shelf=name)
    except sqlite3.Error as exc:
        logger.warning("shelf add failed: %s", exc)
        return _result(False, "add", book=book, shelf=shelf, detail=str(exc))


def remove_from_shelf(book_id, shelf: str) -> dict:
    book = get(book_id)
    if book is None:
        return _result(False, "remove", shelf=shelf,
                       detail="that book isn't in the catalogue")
    if not shelves_available():
        return _result(False, "remove", book=book, shelf=shelf,
                       detail=_SHELF_UNAVAILABLE)
    try:
        with _connect_rw(app_db_path()) as conn:
            row = _find_shelf_row(conn, shelf)
            if row is None:
                return _result(True, "no_such_shelf", book=book, shelf=shelf,
                               detail="there's no shelf by that name")
            name = str(row["name"])
            cur = conn.execute(
                "DELETE FROM book_shelf_link WHERE shelf = ? AND book_id = ?",
                (row["id"], book.id))
            if not cur.rowcount:
                return _result(True, "not_on_shelf", book=book, shelf=name)
            return _result(True, "remove", book=book, shelf=name)
    except sqlite3.Error as exc:
        logger.warning("shelf remove failed: %s", exc)
        return _result(False, "remove", book=book, shelf=shelf, detail=str(exc))


def move(book_id, from_shelf: str, to_shelf: str) -> dict:
    """Take a book off one shelf and put it on another. The ADD is what must
    succeed; a book that was not on the old shelf is still moved, and said so."""
    added = add_to_shelf(book_id, to_shelf)
    if not added["ok"]:
        return added
    detail = ""
    if from_shelf:
        removed = remove_from_shelf(book_id, from_shelf)
        if removed["action"] in ("not_on_shelf", "no_such_shelf"):
            detail = f"it wasn't on {from_shelf}"
    else:
        # "move it to favorites" names no origin, but a MOVE is not a copy: it
        # comes off every other shelf it was on, and we say which.
        landed = added["shelf"]
        off = []
        for name, _count in shelves():
            if _shelf_key(name) == _shelf_key(landed):
                continue
            if int(book_id) in shelf_book_ids(name):
                if remove_from_shelf(book_id, name)["action"] == "remove":
                    off.append(name)
        if off:
            detail = "took it off " + ", ".join(off)
    return _result(True, "move", book=added["book"], shelf=added["shelf"],
                   detail=detail)


# ── "it" / "that one": the last books we named ──────────────────────────────
#
# turn_context holds what the LAST TURN did (its sources, its confirmation
# sentence, the room it named) and pending_clarifier holds an open QUESTION —
# neither carries a list of referents a follow-up can index into ("the second
# one"). So this is the same shape as ``turn_context._ROOMS``: one slot per
# client, a short TTL, in-process only.

_LAST: dict = {}
_last_lock = threading.Lock()


def remember_last(client_id, books) -> None:
    key = str(client_id or "")
    with _last_lock:
        _LAST[key] = {"ts": time.time(),
                      "books": [b for b in (books or []) if isinstance(b, Book)]}


def last(client_id) -> list:
    """The books last named to this client, or [] once they have gone stale."""
    key = str(client_id or "")
    with _last_lock:
        rec = _LAST.get(key)
        if not rec:
            return []
        if time.time() - rec["ts"] > LAST_TTL:
            _LAST.pop(key, None)
            return []
        return list(rec["books"])


def forget_last(client_id=None) -> None:
    """Drop one client's memory, or everything (tests)."""
    with _last_lock:
        if client_id is None:
            _LAST.clear()
        else:
            _LAST.pop(str(client_id or ""), None)


__all__ = [
    "Book", "CATEGORIES", "FORMAT_PREFERENCE", "GLOBAL_TOP", "SAFETY_CAP",
    "SAMPLE_POOL", "MAX_SHELF_NAME", "WRITE_DEADLINE", "browse", "get_many", "LAST_TTL", "POOL_LIMIT",
    "TITLE_MATCH_THRESHOLD", "UMBRELLA_TAGS",
    "add_to_shelf", "app_db_path", "best_match", "catalogue_available",
    "create_shelf", "find", "forget_last", "get", "last", "metadata_db_path",
    "move", "nearest_titles", "popularity", "popularity_path", "recommend",
    "remember_last", "remove_from_shelf", "resolve_category", "shelf_book_ids",
    "shelf_books", "shelf_name", "shelves", "shelves_available",
]
