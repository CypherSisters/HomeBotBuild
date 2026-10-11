"""Tests for browsing a knowledge library (shard-backed paging).

The pathology these guard against: the browse endpoint used to iterate the
whole ZIM directory on every request, so a 8.3M-article Wikipedia pack
never answered at all. Everything here must stay bounded work.
"""

import os
import sqlite3
import time

import pytest

from puffpai.knowledge.fts_index import (
    SCHEMA_VERSION,
    FTSBuilder,
    FTSSearcher,
)
from puffpai.knowledge import shard_browse


# ── Fixture corpus ────────────────────────────────────────────────────────

LONG_BODY = ("Call me Ishmael. " * 40_000)   # > CHUNK_CHARS: stored in parts

CORPUS = [
    ("C/Aardvark", "Aardvark", "The aardvark is a burrowing mammal."),
    ("C/Barnacle", "Barnacle", "Barnacles attach to hulls and rocks."),
    ("C/Cetacean", "Cetacean", "Whales, dolphins and porpoises."),
    ("C/Moby_Dick", "Moby Dick", LONG_BODY),
    ("C/Narwhal", "Narwhal", "The narwhal has a single long tusk."),
    ("C/Octopus", "Octopus", "An octopus has eight arms."),
]

OTHER_CORPUS = [
    ("C/Moby_Dick_notes", "Moby Dick notes", "Notes about the whale novel."),
]


def _make_shard(fts_dir, library_id, records):
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
             ('status', 'complete'),
             ('zim_filename', f'{library_id}.zim'),
             ('zim_size', '1234'),
             ('zim_mtime', '5678'),
             ('article_count', str(count))],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture()
def shard(tmp_path):
    return _make_shard(str(tmp_path / 'fts'), 'sea_pack', CORPUS)


def test_fixture_really_has_part_rows(shard):
    conn = sqlite3.connect(shard)
    try:
        parts = conn.execute(
            "SELECT count(*) FROM docs WHERE instr(path, char(0)) > 0"
        ).fetchone()[0]
        total_rows = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
    finally:
        conn.close()
    assert parts >= 1                      # Moby Dick was chunked
    assert total_rows == len(CORPUS) + parts


# ── Paging ────────────────────────────────────────────────────────────────

def test_total_comes_from_meta_not_a_count(shard):
    page = shard_browse.browse_shard(shard, page=0, limit=2)
    assert page['total'] == len(CORPUS)     # articles, not rows
    assert page['source'] == 'shard'
    assert page['namespace'] == 'C/'


def test_pages_are_in_path_order_and_skip_parts(shard):
    first = shard_browse.browse_shard(shard, page=0, limit=3)
    second = shard_browse.browse_shard(shard, page=1, limit=3)
    assert [a['url'] for a in first['articles']] == [
        'C/Aardvark', 'C/Barnacle', 'C/Cetacean']
    assert [a['url'] for a in second['articles']] == [
        'C/Moby_Dick', 'C/Narwhal', 'C/Octopus']
    assert first['has_more'] is True
    assert second['has_more'] is False
    for art in first['articles'] + second['articles']:
        assert '\x00' not in art['url']
        assert art['source'] == 'shard'
        assert art['mime_type'] == ''


def test_after_cursor_paging_matches_page_paging(shard):
    first = shard_browse.browse_shard(shard, limit=3)
    assert first['next_after'] == first['articles'][-1]['index']
    nxt = shard_browse.browse_shard(shard, limit=3,
                                    after=first['next_after'])
    assert [a['url'] for a in nxt['articles']] == [
        'C/Moby_Dick', 'C/Narwhal', 'C/Octopus']
    assert all('\x00' not in a['url'] for a in nxt['articles'])


def test_cursor_walk_lists_every_article_exactly_once(shard):
    seen, after = [], None
    while True:
        page = shard_browse.browse_shard(shard, limit=2, after=after)
        seen.extend(a['url'] for a in page['articles'])
        after = page['next_after']
        if not page['has_more']:
            break
    assert seen == [rec[0] for rec in CORPUS]


def test_limit_is_capped(shard):
    page = shard_browse.browse_shard(shard, limit=10_000)
    assert len(page['articles']) <= shard_browse.MAX_LIMIT


# ── prefix jump ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("prefix", ["moby", "Moby", "moby dick", "Moby_Dick"])
def test_prefix_jump_lands_on_moby_dick(shard, prefix):
    page = shard_browse.browse_shard(shard, limit=2, prefix=prefix)
    assert page['articles'][0]['url'] == 'C/Moby_Dick'
    assert page['articles'][1]['url'] == 'C/Narwhal'


def test_prefix_past_the_end_returns_nothing(shard):
    page = shard_browse.browse_shard(shard, limit=2, prefix='zzzz')
    assert page['articles'] == []
    assert page['has_more'] is False


def test_prefix_candidates_normalise_titles():
    assert shard_browse.prefix_candidates('moby dick', 'C/') == [
        'C/Moby_dick', 'C/Moby_Dick', 'C/moby dick']


def test_detect_namespace_handles_old_zims(tmp_path):
    db = _make_shard(str(tmp_path / 'fts'), 'old_pack',
                     [("A/Anchor", "Anchor", "An anchor holds a ship.")])
    page = shard_browse.browse_shard(db, limit=5)
    assert page['namespace'] == 'A/'
    assert shard_browse.prefix_candidates('anchor', 'A/')[0] == 'A/Anchor'


# ── q= is a title search, scoped to one library ───────────────────────────

def test_title_search_is_scoped_to_one_library(tmp_path):
    fts_dir = str(tmp_path / 'fts')
    _make_shard(fts_dir, 'sea_pack', CORPUS)
    _make_shard(fts_dir, 'notes_pack', OTHER_CORPUS)
    searcher = FTSSearcher(fts_dir)

    everywhere = searcher.search_titles('moby', max_results=10, per_library=10)
    assert {hit['library_id'] for hit in everywhere} == {'sea_pack',
                                                         'notes_pack'}

    scoped = searcher.search_titles('moby', max_results=10, per_library=10,
                                    libraries=['sea_pack'])
    assert scoped and {hit['library_id'] for hit in scoped} == {'sea_pack'}
    # Parts of the chunked article must not become extra results.
    assert [hit['path'] for hit in scoped] == ['C/Moby_Dick']


# ── No shard: the fallback walk is time-bounded ───────────────────────────

class _Entry:
    def __init__(self, i):
        self.namespace = 'C'
        self.url = f'Article_{i}'
        self.title = f'Article {i}'
        self.mime_type = 'text/html'


def _slow_entries(delay=0.05, count=100_000):
    for i in range(count):
        time.sleep(delay)
        yield _Entry(i)


def test_fallback_walk_stops_at_the_time_budget():
    started = time.monotonic()
    page = shard_browse.browse_zim_bounded(
        _slow_entries(), page=0, limit=200,
        time_budget=shard_browse.ZIM_WALK_BUDGET_S)
    elapsed = time.monotonic() - started

    assert elapsed < 4.0
    assert page['source'] == 'zim'
    assert page['total'] is None
    assert page['has_more'] is True
    assert 0 < len(page['articles']) < 200
    assert all(a['source'] == 'zim' for a in page['articles'])


def test_fallback_walk_stops_when_the_page_is_full():
    entries = (_Entry(i) for i in range(1_000_000))
    page = shard_browse.browse_zim_bounded(entries, page=0, limit=5)
    assert [a['url'] for a in page['articles']] == [
        f'C/Article_{i}' for i in range(5)]
    assert page['total'] is None
    assert page['has_more'] is True
    # The generator must NOT have been drained past the page.
    assert next(entries).url == 'Article_5'


def test_fallback_walk_page_offset():
    page = shard_browse.browse_zim_bounded(
        (_Entry(i) for i in range(50)), page=2, limit=5)
    assert page['articles'][0]['url'] == 'C/Article_10'
    assert page['articles'][0]['index'] == 10


def test_html_entries_walks_once_across_namespaces():
    class _Reader:
        def __init__(self):
            self.calls = []

        def iter_articles(self, namespace='A', mime_filter='text/html'):
            self.calls.append(namespace)
            for i in range(3):
                entry = _Entry(i)
                entry.namespace = 'A'      # an old ZIM: nothing in C
                yield entry

    reader = _Reader()
    got = list(shard_browse.html_entries(reader))
    # One pass, no namespace filter pushed into the reader: walking C first
    # on an A-namespace pack would scan the whole archive to find nothing.
    assert reader.calls == ['']
    assert len(got) == 3


# ── Route wiring (same auth gate as the article route) ────────────────────

@pytest.fixture(scope="module")
def api_mod():
    """Import api_server the way tests/test_api_server.py does."""
    import sys
    from unittest.mock import MagicMock
    from tests.modpatch import patched_modules

    src_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "src", "puffpai")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    with patched_modules({"firewall_manager":
                          MagicMock(FirewallManager=lambda: MagicMock())}):
        import importlib
        mod = (importlib.reload(sys.modules["api_server"])
               if "api_server" in sys.modules
               else importlib.import_module("api_server"))
        mod.app.config['TESTING'] = True
        yield mod


def test_main_route_is_authenticated_like_the_article_route(api_mod):
    # Neither endpoint may be public: the main page is ordinary library
    # content served under the API key.
    assert 'knowledge_get_main_page' not in api_mod._PUBLIC_ENDPOINTS
    assert 'knowledge_get_article' not in api_mod._PUBLIC_ENDPOINTS
    with api_mod.app.test_client() as c:
        assert c.get('/knowledge/library/x/main').status_code == 401


def test_unknown_collection_answers_immediately(api_mod):
    with api_mod.app.test_client() as c:
        headers = {'X-API-Key': api_mod._API_KEY}
        started = time.monotonic()
        resp = c.get('/knowledge/library/no_such_library/articles?limit=5',
                     headers=headers)
        assert time.monotonic() - started < 3.0
        assert resp.status_code == 200
        body = resp.get_json()
        assert body == {'success': True, 'articles': [], 'total': 0,
                        'has_more': False, 'next_after': None,
                        'source': 'none',
                        'message': 'No browsable ZIM file found for this '
                                   'collection'}

        resp = c.get('/knowledge/library/no_such_library/main',
                     headers=headers)
        assert resp.status_code == 404
        assert resp.get_json()['success'] is False


def test_article_links_and_assets_are_rewritten_together(api_mod):
    out = api_mod._rewrite_zim_article_html(
        '<a href="../C/Whale#Sec">w</a><img src="../I/w.png">',
        'wiki', 'C/Moby_Dick')
    assert 'data-zim-url="C/Whale"' in out
    assert 'data-zim-fragment="Sec"' in out
    # The asset pass must not have turned the article link into an asset
    # URL, and must still have rewritten the image.
    assert 'asset?path=C/Whale' not in out
    assert 'asset?path=I/w.png' in out


# ══════════════════════════════════════════════════════════════════════════
#  Regressions found under adversarial testing
#
#  browse_shard used a plain sqlite3.connect, which CREATES the file when
#  it is missing: a typo'd collection left a zero-byte .db in the FTS
#  directory and then raised "no such table: docs", which the route turned
#  into an HTTP 500. A shard we cannot read is an empty page, not a fault.
# ══════════════════════════════════════════════════════════════════════════

_EMPTY_PAGE_KEYS = {'articles', 'total', 'has_more', 'next_after',
                    'namespace', 'source', 'message'}


def _assert_empty_page(page):
    assert set(page) == _EMPTY_PAGE_KEYS
    assert page['articles'] == []
    assert page['total'] is None
    assert page['has_more'] is False
    assert page['next_after'] is None
    assert page['source'] == 'none'
    assert isinstance(page['message'], str) and page['message']


def test_B1_missing_shard_creates_no_file_and_does_not_raise(tmp_path):
    missing = str(tmp_path / "does_not_exist.db")
    page = shard_browse.browse_shard(missing, limit=5)
    _assert_empty_page(page)
    # The old code left a zero-byte database behind on every bad request.
    assert not os.path.exists(missing)
    assert list(tmp_path.iterdir()) == []


def test_B1_missing_shard_stays_missing_across_every_entry_point(tmp_path):
    missing = str(tmp_path / "nope.db")
    for kwargs in ({}, {'page': 3}, {'after': 10}, {'prefix': 'Moby'}):
        _assert_empty_page(shard_browse.browse_shard(missing, **kwargs))
    assert not os.path.exists(missing)


def test_B2_a_directory_is_not_a_500(tmp_path):
    folder = tmp_path / "a_folder"
    folder.mkdir()
    _assert_empty_page(shard_browse.browse_shard(str(folder), limit=5))


def test_B2_a_corrupt_file_is_not_a_500(tmp_path):
    junk = tmp_path / "corrupt.db"
    junk.write_bytes(b"this is not a sqlite database, not even close" * 40)
    _assert_empty_page(shard_browse.browse_shard(str(junk), limit=5))


def test_B2_an_empty_file_is_not_a_500(tmp_path):
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    _assert_empty_page(shard_browse.browse_shard(str(empty), limit=5))


def test_B2_a_database_without_our_tables_is_not_a_500(tmp_path):
    other = tmp_path / "other.db"
    conn = sqlite3.connect(str(other))
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    _assert_empty_page(shard_browse.browse_shard(str(other), limit=5))


def test_B2_a_shard_missing_its_meta_table_is_not_a_500(tmp_path):
    half = tmp_path / "half.db"
    conn = sqlite3.connect(str(half))
    conn.execute("CREATE TABLE docs (id INTEGER PRIMARY KEY, "
                 "path TEXT, title TEXT)")
    conn.execute("INSERT INTO docs (path, title) VALUES ('C/A', 'A')")
    conn.commit()
    conn.close()
    _assert_empty_page(shard_browse.browse_shard(str(half), limit=5))


def test_browse_never_writes_to_a_good_shard(shard):
    before = os.stat(shard)
    page = shard_browse.browse_shard(shard, limit=2)
    assert page['articles']
    after = os.stat(shard)
    assert (before.st_size, before.st_mtime_ns) == (after.st_size,
                                                    after.st_mtime_ns)
    # No WAL/journal sidecars either.
    folder = os.path.dirname(shard)
    assert not [f for f in os.listdir(folder)
                if f.endswith(('-wal', '-shm', '-journal'))]


def test_connection_is_genuinely_read_only(shard):
    conn = shard_browse._connect_ro(shard)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO docs (path, title) VALUES ('C/X','X')")
    finally:
        conn.close()


def test_shard_uri_survives_a_path_needing_quoting(tmp_path):
    odd = tmp_path / "lib #1 ?weird& name.db"
    uri = shard_browse._shard_uri(str(odd))
    assert uri.startswith('file:')
    assert uri.endswith('?mode=ro')
    # The '?' and '#' in the filename must be quoted, or SQLite reads them
    # as URI syntax and silently opens the wrong database.
    assert uri.count('?') == 1 and '#' not in uri
    _assert_empty_page(shard_browse.browse_shard(str(odd), limit=2))
    assert not os.path.exists(str(odd))


def test_unreadable_shard_answers_200_not_500(api_mod, tmp_path,
                                              monkeypatch):
    missing = str(tmp_path / "ghost.db")
    monkeypatch.setattr(api_mod, '_shard_path_for_collection',
                        lambda collection: ('ghost', missing))
    with api_mod.app.test_client() as c:
        resp = c.get('/knowledge/library/ghost/articles?limit=5',
                     headers={'X-API-Key': api_mod._API_KEY})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['success'] is True
    assert body['articles'] == []
    assert body['source'] == 'none'
    assert body['message']
    assert not os.path.exists(missing)


# ── The asset endpoint never forwards the pack's MIME string ─────────────
#
# These
# values passed the old "(?:;.*)?$" regex, went out inline with no
# Content-Disposition or CSP, and headless Chrome ran the script inside.

class _FakeAsset:
    def __init__(self, mime):
        self.mime_type = mime
        self.content = b'<script>alert(1)</script>'
        self.title = 't'
        self.namespace = 'C'
        self.url = 'x'


def _serve_asset(api_mod, monkeypatch, mime, headers=None):
    class _Reader:
        def get_article_by_url(self, url):
            return _FakeAsset(mime)
    monkeypatch.setattr(api_mod, '_get_zim_reader_for_collection',
                        lambda collection: _Reader())
    with api_mod.app.test_client() as c:
        return c.get('/knowledge/library/coll/asset?path=x',
                     headers={'X-API-Key': api_mod._API_KEY,
                              **(headers or {})})


@pytest.mark.parametrize("mime", [
    'image/png;a,text/html',
    'text/css;,text/html',
    'video/mp4;x="a",text/html',
    'image/png,text/html',
    'audio/ogg;x="y,text/html"',
    'text/html',
    'image/svg+xml',
    'application/xhtml+xml',
    'text/css',
    'application/pdf',
    'text/vtt;,text/html',
    None,
])
def test_asset_that_could_run_downloads_as_octet_stream(api_mod, monkeypatch,
                                                        mime):
    resp = _serve_asset(api_mod, monkeypatch, mime)
    assert resp.status_code == 200
    assert resp.headers['Content-Type'] == 'application/octet-stream'
    assert resp.headers['Content-Disposition'] == 'attachment'
    assert resp.headers['X-Content-Type-Options'] == 'nosniff'
    assert resp.headers['Content-Security-Policy'] == (
        "default-src 'none'; sandbox")


@pytest.mark.parametrize("mime,clean", [
    ('image/png', 'image/png'),
    ('image/png; q=1', 'image/png'),
    ('IMAGE/JPG', 'image/jpeg'),
    ('video/webm; codecs=vp9', 'video/webm'),
    ('font/woff2', 'font/woff2'),
    ('text/vtt', 'text/vtt'),
    ('text/vtt; charset=utf-8', 'text/vtt'),
])
def test_displayable_asset_is_inline_with_the_clean_type(api_mod, monkeypatch,
                                                         mime, clean):
    resp = _serve_asset(api_mod, monkeypatch, mime)
    assert resp.status_code == 200
    assert resp.headers['Content-Type'] == clean
    assert 'Content-Disposition' not in resp.headers
    assert resp.headers['X-Content-Type-Options'] == 'nosniff'
    assert resp.headers['Content-Security-Policy'] == (
        "default-src 'none'; sandbox")


def test_smuggled_media_type_gets_no_range_support(api_mod, monkeypatch):
    resp = _serve_asset(api_mod, monkeypatch, 'video/mp4;x="a",text/html',
                        headers={'Range': 'bytes=0-3'})
    assert resp.status_code == 200
    assert 'Accept-Ranges' not in resp.headers
    assert resp.headers['Content-Type'] == 'application/octet-stream'


def test_asset_not_found_carries_the_same_headers(api_mod, monkeypatch):
    class _Empty:
        def get_article_by_url(self, url):
            return None
    monkeypatch.setattr(api_mod, '_get_zim_reader_for_collection',
                        lambda collection: _Empty())
    with api_mod.app.test_client() as c:
        resp = c.get('/knowledge/library/coll/asset?path=x',
                     headers={'X-API-Key': api_mod._API_KEY})
    assert resp.status_code == 404
    assert resp.headers['Content-Type'].startswith('text/plain')
    assert resp.headers['X-Content-Type-Options'] == 'nosniff'
    assert resp.headers['Content-Security-Policy'] == (
        "default-src 'none'; sandbox")


# ── The article helper sanitises, and fails closed ────────────────────────

def test_article_helper_strips_script_urls_and_handlers(api_mod):
    out = api_mod._rewrite_zim_article_html(
        '<a title="a>b" href="javascript:alert(1)">x</a>'
        '<img src="../I/w.png" onerror="steal()">',
        'wiki', 'C/Moby_Dick')
    assert 'javascript' not in out
    assert 'onerror' not in out
    assert 'asset?path=I/w.png' in out


def test_article_helper_fails_closed_when_the_rewrite_raises(api_mod,
                                                             monkeypatch):
    import puffpai.knowledge.zim_links as zim_links
    monkeypatch.setattr(zim_links, 'rewrite_article_links',
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError('boom')))
    out = api_mod._rewrite_zim_article_html(
        '<a href="javascript:alert(1)">x</a>', 'wiki', 'C/Moby_Dick')
    # Escaped, so the browser renders source text instead of a live link.
    assert '<a' not in out
    assert '&lt;a href=' in out
