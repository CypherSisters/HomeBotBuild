"""02/04/06/07 + — the library must answer, not the model.

Every phrasing below is a real user phrasing that, without these fastpaths,
is answered by RAG, by an unrelated widget, or by the whole Home Assistant
entity inventory while the real Jellyfin/Calibre data sits one call away:

   / "show me Casablanca"
                         "show me the movie Casablanca on the tv"
                "do we have any Jane Austen books"
                "do we have Back to the Future"
                "what playlists do I have saved"

Fixtures are shaped like the REAL responses of a running install:
the Jellyfin ``/Items`` movie payload (Name/Id/Genres/ProductionYear/
RunTimeTicks/Type) and a real Calibre ``metadata.db`` schema subset.
own console lines (``|> Station "…"`` and the ``# -04:13/04:14`` position
line that freezes when playback pauses). No network, no docker, no Jellyfin.

Run: py -3 -m pytest tests/test_media_library_answers.py -q
"""

from __future__ import annotations

import sqlite3

import pytest

from puffpai import media_library as ml


# ── Jellyfin fixtures (real /Items shape) ─────────────────────────────────────

def _jf_movie(name, item_id, year, genres):
    return {
        "Name": name, "Id": item_id, "Type": "Movie",
        "ServerId": "0000000000000000000000000000000a",
        "ProductionYear": year, "Genres": list(genres),
        "RunTimeTicks": 61226226230,
        "Path": f"/mnt/media/movies/{name}/{name}.mkv",
        "Overview": "…", "IsFolder": False, "MediaType": "Video",
    }


LIBRARY = [
    _jf_movie("Casablanca", "15a10f31298d5e6e41d4872131f6274a", 1942,
              ["Drama", "Romance"]),
    _jf_movie("Back to the Future", "22b20f31298d5e6e41d4872131f6274b", 1985,
              ["Adventure", "Comedy"]),
    _jf_movie("Back to the Future Part II", "33c30f31298d5e6e41d4872131f6274c",
              1989, ["Adventure", "Comedy"]),
    _jf_movie("Nosferatu", "44d40f31298d5e6e41d4872131f6274d", 1922, ["Horror"]),
    _jf_movie("Night of the Living Dead", "55e50f31298d5e6e41d4872131f6274e",
              1968, ["Horror"]),
]

PLAYLISTS = [
    {"Name": "Road Trip", "Id": "aa1", "Type": "Playlist", "ChildCount": 42},
    {"Name": "Dinner", "Id": "bb2", "Type": "Playlist", "ChildCount": 0},
]


class FakeJellyfin:
    """A MediaBridge with only its HTTP hop replaced, so the real
    search_movies/list_playlists/_jf_movie_to_item code is exercised."""

    def __init__(self, items=None, playlists=None, reachable=True,
                 calibre_db=None):
        from puffpai.media_bridge import MediaBridge
        self._b = MediaBridge()
        self._items = LIBRARY if items is None else items
        self._playlists = PLAYLISTS if playlists is None else playlists
        self._reachable = reachable
        self._b._jf_request = self._request           # type: ignore[assignment]
        self._b._get_user_id = lambda: "user-1"       # type: ignore[assignment]
        if calibre_db is not None:
            self._b._calibre_db = str(calibre_db)

    def _request(self, path, method="GET", params=None, data=None):
        from puffpai.media_bridge import MediaBridgeError
        if not self._reachable:
            raise MediaBridgeError("Cannot reach Jellyfin at http://…:8096.")
        params = params or {}
        kind = params.get("IncludeItemTypes")
        if kind == "Playlist":
            return {"Items": list(self._playlists),
                    "TotalRecordCount": len(self._playlists)}
        if path == "/System/Info/Public":
            return {"ServerName": "puffpai-jellyfin", "Version": "10.11.6"}
        term = str(params.get("SearchTerm") or "").lower()
        genre = str(params.get("Genres") or "")
        hits = [i for i in self._items
                if (not term or term in i["Name"].lower())
                and (not genre or genre in i["Genres"])]
        return {"Items": hits, "TotalRecordCount": len(hits)}

    def __getattr__(self, name):
        return getattr(self._b, name)


@pytest.fixture
def jf(monkeypatch):
    bridge = FakeJellyfin()
    monkeypatch.setattr("puffpai.media_bridge.get_media_bridge",
                        lambda **_kw: bridge)
    return bridge


@pytest.fixture
def display(monkeypatch, tmp_path):
    """dashboard_display writing to a throwaway state file."""
    from puffpai import dashboard_display as dd
    monkeypatch.setattr(dd, "_STATE_PATH", str(tmp_path / "display.json"))
    dd.clear_display(source="test")
    return dd


# ── / — "show me <real movie title>" ─────────────────────

@pytest.mark.parametrize("phrase", [
    "show me Casablanca",
    "show me the movie Casablanca on the tv",
])
def test_show_me_a_real_title_resolves_against_jellyfin(phrase, jf, display,
                                                        monkeypatch):
    import puffpai.api_server as api
    reply = api._try_media_show(phrase)
    assert reply is not None, "the show fastpath must claim a real library title"
    assert "Casablanca" in reply
    item = display.get_display()
    assert item is not None, "the kiosk overlay must get a viewer payload"
    assert item["kind"] == display.KIND_URL
    assert item["url"].startswith(ml.MOVIE_VIEWER_ROUTE)
    assert item["url"].startswith("/"), "the overlay iframes same-origin only"
    assert item["meta"]["item_id"] == "15a10f31298d5e6e41d4872131f6274a"
    assert item["meta"]["source"] == "jellyfin"
    assert item["meta"]["media_type"] == "movie"


def test_show_me_a_title_we_do_not_have_says_so_and_offers_the_closest(jf,
                                                                       display):
    import puffpai.api_server as api
    reply = api._try_media_show("show me the movie Jurassic Park on the tv")
    assert reply is not None
    assert "don't have Jurassic Park" in reply
    assert "closest" in reply.lower()
    assert display.get_display() is None, "a miss must not queue a viewer"


def test_show_me_a_title_never_answers_with_trivia(jf, display):
    """Never a Wikipedia plot summary in place of the movie."""
    import puffpai.api_server as api
    reply = api._try_media_show("show me Casablanca")
    for word in ("wikipedia", "novel by", "directed by", "plot"):
        assert word not in reply.lower()


def test_show_me_declines_when_it_is_not_a_library_title(jf, display):
    """Everything that is not a movie keeps its existing path."""
    import puffpai.api_server as api
    for phrase in ("show me the TED talk about surveillance",
                   "show me my calendar",
                   "pull up the weather",
                   "show me some horror movies",
                   "show me a birth certificate"):
        assert api._try_media_show(phrase) is None, phrase


def test_show_me_says_so_when_the_media_server_is_unreachable(monkeypatch,
                                                              display):
    import puffpai.api_server as api
    bridge = FakeJellyfin(reachable=False)
    monkeypatch.setattr("puffpai.media_bridge.get_media_bridge",
                        lambda **_kw: bridge)
    reply = api._try_media_show("show me the movie Casablanca")
    assert reply == ml.LIBRARY_UNAVAILABLE_MSG


def test_fuzzy_titles_still_resolve(jf, display):
    """Case, articles and punctuation drift must not lose a real title."""
    assert ml.find_movie("casablanca")["status"] == "match"
    assert ml.find_movie("Casablanca!")["status"] == "match"
    assert ml.normalize_title("The Dark Tower") == "dark tower"
    # …but a near neighbour is NOT the same film.
    got = ml.find_movie("Back to the Future")
    assert ml.label_movie(got["match"]) == "Back to the Future (1985)"


# ── — a direct title lookup ────────────────────────────────────────

def test_do_we_have_a_real_title_answers_from_the_library(jf):
    import puffpai.api_server as api
    reply = api._try_media_library_query("do we have Back to the Future")
    assert reply is not None, "the title lookup must not fall through to RAG"
    assert "Back to the Future (1985)" in reply
    assert "yes" in reply.lower()
    # A RAG answer here would be built from an unrelated uploaded document.
    assert "workout" not in reply.lower() and "fitness" not in reply.lower()


def test_do_we_have_leaves_non_library_questions_alone(jf):
    import puffpai.api_server as api
    for phrase in ("do we have any milk",
                   "do we have any horror movies",
                   "do we have enough water for three days",
                   "do we have hot dogs"):
        assert api._try_media_library_query(phrase) is None, phrase


def test_a_movie_framed_miss_is_honest(jf):
    import puffpai.api_server as api
    reply = api._try_media_library_query("do we have the movie Jurassic Park")
    assert reply is not None and "don't have Jurassic Park" in reply


# ── — the real Calibre catalogue ───────────────────────────────────

@pytest.fixture
def calibre(tmp_path):
    """A real Calibre metadata.db (the columns media_bridge actually reads)."""
    db = tmp_path / "metadata.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, path TEXT,"
        " timestamp TEXT);"
        "CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE books_authors_link (id INTEGER PRIMARY KEY, book INTEGER,"
        " author INTEGER);"
    )
    conn.execute("INSERT INTO authors VALUES (1, 'Jane Austen')")
    conn.execute("INSERT INTO authors VALUES (2, 'Stephen King')")
    for bid, title, aid in ((1, "Emma", 1), (2, "Persuasion", 1),
                            (3, "The Gunslinger", 2)):
        conn.execute("INSERT INTO books VALUES (?, ?, ?, '2026-01-01')",
                     (bid, title, f"Author/{title}"))
        conn.execute("INSERT INTO books_authors_link VALUES (?, ?, ?)",
                     (bid, bid, aid))
    conn.commit()
    conn.close()
    return db


def test_book_question_reads_the_real_catalogue(monkeypatch, calibre):
    import puffpai.api_server as api
    bridge = FakeJellyfin(calibre_db=calibre)
    monkeypatch.setattr("puffpai.media_bridge.get_media_bridge",
                        lambda **_kw: bridge)
    reply = api._try_media_library_query("do we have any Jane Austen books")
    assert reply is not None, "book questions must not fall through to RAG"
    assert "Emma" in reply and "Persuasion" in reply
    assert "The Gunslinger" not in reply
    # A RAG answer here invents lending services this system does not have.
    low = reply.lower()
    assert "libby" not in low and "gutenberg" not in low


def test_book_question_says_so_when_calibre_is_unreachable(monkeypatch,
                                                           tmp_path):
    import puffpai.api_server as api
    bridge = FakeJellyfin(calibre_db=tmp_path / "does-not-exist.db")
    monkeypatch.setattr("puffpai.media_bridge.get_media_bridge",
                        lambda **_kw: bridge)
    reply = api._try_media_library_query("do we have any Jane Austen books")
    assert reply == ml.BOOKS_UNAVAILABLE_MSG


def test_book_question_with_no_match_never_invents_one(monkeypatch, calibre):
    import puffpai.api_server as api
    bridge = FakeJellyfin(calibre_db=calibre)
    monkeypatch.setattr("puffpai.media_bridge.get_media_bridge",
                        lambda **_kw: bridge)
    reply = api._try_media_library_query("do we have any Toni Morrison books")
    assert reply is not None
    assert "don't have anything by Toni Morrison" in reply


# ── — saved playlists, not the HA device inventory ─────────────────

def test_what_playlists_do_i_have_saved_reads_the_playlist_store(jf,
                                                                 monkeypatch):
    import puffpai.api_server as api
    monkeypatch.setattr("puffpai.spotify_control.get_control",
                        lambda: _NoSpotify())
    reply = api._try_media_library_query("what playlists do I have saved")
    assert reply is not None, "the playlist read must not reach list_devices"
    assert "Road Trip" in reply and "Dinner" in reply
    # The wrong route dumps the whole Home Assistant entity inventory.
    low = reply.lower()
    assert "device" not in low and "media_player." not in low


def test_no_playlists_says_none_saved(monkeypatch):
    import puffpai.api_server as api
    bridge = FakeJellyfin(playlists=[])
    monkeypatch.setattr("puffpai.media_bridge.get_media_bridge",
                        lambda **_kw: bridge)
    monkeypatch.setattr("puffpai.spotify_control.get_control",
                        lambda: _NoSpotify())
    reply = api._try_media_library_query("what playlists do I have saved")
    assert "don't have any playlists saved" in reply


def test_an_unreachable_playlist_source_is_not_reported_as_empty(monkeypatch):
    import puffpai.api_server as api
    bridge = FakeJellyfin(reachable=False)
    monkeypatch.setattr("puffpai.media_bridge.get_media_bridge",
                        lambda **_kw: bridge)
    monkeypatch.setattr("puffpai.spotify_control.get_control",
                        lambda: _NoSpotify())
    reply = api._try_media_library_query("what playlists do I have saved")
    assert "couldn't reach" in reply.lower()
    assert "don't have any playlists" not in reply


def test_playing_a_playlist_is_not_a_playlist_read(jf):
    import puffpai.api_server as api
    assert api._try_media_library_query("play my Road Trip playlist") is None


class _NoSpotify:
    def is_configured(self):
        return False


# ── the corpus these fastpaths must NEVER claim ─────────────────────────────

def test_ordinary_sentences_never_reach_the_library_fastpaths(jf, display):
    """Same discipline as the capability-read gate: a fastpath is only as good
    as the corpus it refuses. Run against a REACHABLE fake library so a decline
    is a real decline, not an unreachable-server accident."""
    import puffpai.api_server as api
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(__file__), "test_capread_dispatch.py")
    spec = importlib.util.spec_from_file_location("_capread_corpus", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    corpus = list(mod.DECLINE_CORPUS) + list(mod.ACTION_TURNS) + [
        "show me the news", "do we have plans this weekend",
        "play some jazz radio", "stop the music", "what is on tv tonight",
        "do we have any eggs left", "show me my documents",
    ]
    claimed = [(s, r) for s in corpus
               for r in (api._try_media_library_query(s),
                         api._try_media_show(s)) if r]
    assert claimed == [], f"claimed turns it should decline: {claimed[:5]}"
