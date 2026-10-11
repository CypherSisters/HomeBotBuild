"""Browsing a knowledge library without scanning it.

Answering "list me page N of this library" by iterating the ZIM directory
from entry 0 on every request means walking 26.6 million entries for
``wikipedia_en_all_maxi``, purely to compute a ``total`` the caller barely
uses. Such a request never returns and leaves the API process spinning at
100% CPU after the client gives up.

Everything the browse list needs is already in the FTS shard
(:mod:`puffpai.knowledge.fts_index`): ``docs.id`` is assigned in ZIM
directory order (which is path order, i.e. alphabetical for wiki packs),
``docs.path`` is UNIQUE and therefore indexed, and ``meta.article_count``
is the total. So a page is a keyset window over an indexed table and a
prefix jump is one index seek — both constant-ish work regardless of how
many million articles the pack holds.

Libraries with no shard fall back to a *bounded* directory walk: it stops
as soon as the page is full or the time budget expires, and reports
``total = None``. No code path here may scan a whole archive.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import time
from typing import Any, Iterable, Iterator, Optional

# Part rows of a chunked article are keyed "<path>\x00<N>" (fts_index
# _PART_SEP). They are the same article and must never appear in a list.
_NO_PART_ROWS = "instr(path, char(0)) = 0"

# A page may not be larger than this however the caller asks.
MAX_LIMIT = 200

# Wall-clock ceiling for the shard-less fallback walk.
ZIM_WALK_BUDGET_S = 3.0


def _shard_uri(db_path: str) -> str:
    """``file:`` URI for ``db_path``, percent-quoted, read-only.

    ``pathlib`` does the quoting (``?``, ``#`` and spaces in a library name
    would otherwise be read as URI syntax) and produces the
    ``file:///C:/...`` form SQLite wants for a Windows drive path.
    """
    return pathlib.Path(os.path.abspath(db_path)).as_uri() + "?mode=ro"


def _connect_ro(db_path: str) -> sqlite3.Connection:
    """Open a shard strictly read-only.

    ``mode=ro`` matters twice: a browse request must never write to a shard
    the indexer may be building, and a plain ``sqlite3.connect`` on a
    missing path *creates* an empty database file — so a typo'd collection
    name would litter the FTS directory with zero-byte ``.db`` files and
    then fail with "no such table: docs".
    """
    conn = sqlite3.connect(_shard_uri(db_path), uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _unavailable(message: str) -> dict[str, Any]:
    """The answer for a shard we cannot read: an empty page, not an error.

    Mirrors what the route already returns for an unknown collection, so a
    missing, corrupt or unreadable shard is an honest empty list with a
    message at HTTP 200 rather than a 500 that tells the reader nothing.
    """
    return {'articles': [], 'total': None, 'has_more': False,
            'next_after': None, 'namespace': '', 'source': 'none',
            'message': message}


def detect_namespace(conn: sqlite3.Connection) -> str:
    """Namespace prefix used by this shard's paths: ``'C/'``, ``'A/'`` or ``''``.

    Read from the first row rather than configured: old ZIMs put articles in
    ``A/`` and ZIM 6 in ``C/``, and a shard only ever holds one of them.
    """
    row = conn.execute(
        f"SELECT path FROM docs WHERE {_NO_PART_ROWS} ORDER BY id LIMIT 1"
    ).fetchone()
    path = (row['path'] if row else '') or ''
    head = path.split('/', 1)[0]
    return f"{head}/" if len(head) == 1 and head.isalpha() else ''


def prefix_candidates(prefix: str, namespace: str) -> list[str]:
    """Path prefixes to try for a ``prefix=`` jump, best guess first.

    Wiki article paths spell a title with underscores and an upper-case
    first letter ("moby dick" -> ``C/Moby_dick``), so the normalised form is
    tried first; the raw form covers packs that key entries literally.
    """
    raw = prefix.strip()
    if not raw:
        return []
    underscored = raw.replace(' ', '_')
    forms = [underscored[0].upper() + underscored[1:],
             # Multi-word titles are often capitalised per word
             # ("moby dick" -> "Moby_Dick"), and docs.path compares
             # case-SENSITIVELY, so the two spellings land in different
             # places in the index and both must be probed.
             '_'.join(w[:1].upper() + w[1:] for w in underscored.split('_')),
             raw]
    seen: list[str] = []
    for form in forms:
        if form not in seen:
            seen.append(form)
    return [f"{namespace}{form}" for form in seen]


def _resolve_prefix(conn: sqlite3.Connection, prefix: str,
                    namespace: str) -> Optional[int]:
    """Return the ``docs.id`` to start a prefix jump *after* (id - 1).

    Uses the UNIQUE index on ``path`` (``path >= ?``), so this is a seek,
    not a scan. A candidate whose first row actually starts with it wins;
    otherwise the nearest row to the preferred form is used, which lands the
    reader next to what they typed instead of failing.
    """
    nearest: Optional[int] = None
    for candidate in prefix_candidates(prefix, namespace):
        row = conn.execute(
            f"SELECT id, path FROM docs WHERE path >= ? AND {_NO_PART_ROWS} "
            "ORDER BY path LIMIT 1",
            (candidate,),
        ).fetchone()
        if row is None:
            continue
        if nearest is None:
            nearest = row['id']
        if row['path'].startswith(candidate):
            return row['id']
    return nearest


def browse_shard(db_path: str, *, page: int = 0, limit: int = 50,
                 after: Optional[int] = None,
                 prefix: str = '') -> dict[str, Any]:
    """One page of a library's article list, read from its FTS shard.

    ``after`` is a keyset cursor (``docs.id``) and is what deep paging
    should use; ``page`` stays supported for the existing dashboard, and
    costs an OFFSET proportional to the page number only.

    Returns ``{articles, total, has_more, next_after, namespace, source}``.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    page = max(0, int(page))

    try:
        conn = _connect_ro(db_path)
    except (sqlite3.Error, OSError, ValueError) as exc:
        # Missing file, a directory, no read permission: all of them mean
        # "no library here", none of them is a server fault.
        return _unavailable("This library's search index is not readable "
                            f"({exc.__class__.__name__}).")
    try:
        namespace = detect_namespace(conn)
        total_row = conn.execute(
            "SELECT value FROM meta WHERE key = 'article_count'").fetchone()
        try:
            total = int(total_row['value']) if total_row else None
        except (TypeError, ValueError):
            total = None

        offset = 0
        if prefix:
            start = _resolve_prefix(conn, prefix, namespace)
            if start is None:
                # Nothing at or after this prefix: the reader asked past the
                # end of the library, which is an empty page, not page 0.
                return {'articles': [], 'total': total, 'has_more': False,
                        'next_after': None, 'namespace': namespace,
                        'source': 'shard'}
            after = start - 1
        elif after is None:
            offset = page * limit

        # limit + 1 rows: the extra row IS the has_more answer, so no
        # COUNT(*) and no second query.
        rows = conn.execute(
            f"SELECT id, path, title FROM docs "
            f"WHERE id > ? AND {_NO_PART_ROWS} ORDER BY id LIMIT ? OFFSET ?",
            (after if after is not None else 0, limit + 1, offset),
        ).fetchall()
    except sqlite3.Error as exc:
        # Truncated or corrupt file, or a database without our schema.
        return _unavailable("This library's search index is unreadable or "
                            f"incomplete ({exc.__class__.__name__}).")
    finally:
        conn.close()

    has_more = len(rows) > limit
    rows = rows[:limit]
    articles = [{
        'index': row['id'],
        'title': row['title'] or row['path'],
        'url': row['path'],
        # Shards do not record per-entry mime types; the key stays for
        # response-shape compatibility with the old ZIM-walk response.
        'mime_type': '',
        'source': 'shard',
    } for row in rows]

    return {
        'articles': articles,
        'total': total,
        'has_more': has_more,
        'next_after': rows[-1]['id'] if rows else None,
        'namespace': namespace,
        'source': 'shard',
    }


def browse_zim_bounded(entries: Iterable[Any], *, page: int = 0,
                       limit: int = 50,
                       time_budget: float = ZIM_WALK_BUDGET_S,
                       clock=time.monotonic) -> dict[str, Any]:
    """Fallback page for a library with no shard: a *bounded* directory walk.

    Stops the moment the requested page is full or ``time_budget`` seconds
    have passed, whichever comes first. ``total`` is therefore unknown
    (``None``) and ``has_more`` is optimistic when the walk was cut short —
    an honest "there may be more" beats a number that costs 90 seconds.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    page = max(0, int(page))
    offset = page * limit
    deadline = clock() + time_budget

    articles: list[dict[str, Any]] = []
    seen = 0
    truncated = False
    for entry in entries:
        if clock() >= deadline:
            truncated = True
            break
        seen += 1
        if seen <= offset:
            continue
        articles.append({
            'index': seen - 1,
            'title': entry.title,
            'url': f"{entry.namespace}/{entry.url}",
            'mime_type': entry.mime_type,
            'source': 'zim',
        })
        if len(articles) >= limit:
            truncated = True
            break

    return {
        'articles': articles,
        'total': None,
        'has_more': truncated,
        'next_after': None,
        'namespace': '',
        'source': 'zim',
    }


def html_entries(reader: Any, namespaces: Iterable[str] = ('C', 'A'),
                 mime_filter: str = 'text/html') -> Iterator[Any]:
    """Lazily yield article entries from either article namespace.

    Deliberately ONE pass with the namespace filter applied here rather than
    a pass per namespace inside the reader: ZIM 6 keys articles under ``C``
    and ZIM 5 under ``A``, and walking ``C`` first on a ZIM 5 pack would
    scan the entire archive just to learn that ``C`` is empty — the exact
    cost this module exists to remove. Yielding from one pass also means the
    caller's clock is checked once per candidate entry.
    """
    wanted = set(namespaces)
    for entry in reader.iter_articles(namespace='', mime_filter=mime_filter):
        if entry.namespace in wanted:
            yield entry
