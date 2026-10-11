"""The file the owner sent, and a playlist built out of it.

A question about an uploaded screenshot that is already ingested by name is
answered by the files fastpath, not by a neighbouring feature:

* "photo" next to "i sent you" means the upload, not a security camera
  snapshot, and a misspelled file name still finds the file;
* "a file called Station Guide" is read back from the upload, not answered by
  the registry capability read with a list of radio stations.

The accept half of these tests covers those phrasings, one with a typo in the
file name, and the decline half is every neighbouring phrasing that must keep
its own path (the camera, radio playback, the librarian). The precedence test
pins the order that keeps the second case right: the files fastpath runs before
a capability READ.

Run: py -3 -m pytest tests/test_media_files_taste.py -q
"""

from __future__ import annotations

import ast
import inspect
import re

import pytest

from puffpai import api_server, capability_registry, document_lookup
from puffpai.media_bridge import MediaBridge, MediaBridgeError, MediaItem
from puffpai.media_bridge import parse_artist_names


# ── parse_artist_names ──────────────────────────────────────────────────────

#: What OCR really returns for a phone screenshot of a station list.
OCR_SAMPLE = "0) q Coldplay Radio\n1) q Glass Animals Radio\n2) Q QuickMix"


def test_parse_artist_names_on_the_ocr_sample():
    assert parse_artist_names(OCR_SAMPLE) == ["Coldplay", "Glass Animals"]


def test_parse_artist_names_drops_ui_furniture():
    text = ("Thumbprint Radio\nShuffle\nMy Stations\nNow Playing\nSort\n"
            "Alt-J\nCollection")
    assert parse_artist_names(text) == ["Alt-J"]


def test_parse_artist_names_keeps_a_number_that_is_part_of_the_name():
    assert parse_artist_names("3 Doors Down\n1. Wilco") == ["3 Doors Down",
                                                            "Wilco"]


def test_parse_artist_names_dedupes_and_caps():
    assert parse_artist_names("Wilco, wilco, WILCO") == ["Wilco"]
    assert len(parse_artist_names("\n".join(f"Band {n}" for n in
                                            range(100)))) == 40


def test_parse_artist_names_drops_junk_and_survives_nothing():
    assert parse_artist_names("") == []
    assert parse_artist_names("*** ///\n#\n1") == []


# ── build_taste_playlist ────────────────────────────────────────────────────

LIBRARY_ARTISTS = [
    {"id": "a1", "name": "Coldplay", "album_count": 4},
    {"id": "a2", "name": "Glass Animals", "album_count": 2},
    {"id": "a3", "name": "Wilco", "album_count": 6},
]


def _track(item_id, artist, title="Song"):
    return MediaItem(item_id=item_id, title=title, media_type="music",
                     artist=artist)


def make_bridge(*, artists=None, tracks=None, mixes=None, playlists=(),
                create=None, on_search=None):
    """A MediaBridge whose every network method is a stub. No sockets.

    Built with ``object.__new__`` so the methods under test — the real
    ``build_taste_playlist`` and ``_free_playlist_name`` — run unchanged.
    """
    bridge = object.__new__(MediaBridge)
    catalogue = tracks if tracks is not None else {
        "Coldplay": [_track("c1", "Coldplay"), _track("c2", "Coldplay")],
        "Glass Animals": [_track("g1", "Glass Animals")],
        "Wilco": [_track("w1", "Wilco")],
    }
    bridge.calls = []

    def _search(query="", *, artist="", album="", genre="", limit=20):
        if on_search is not None:
            return on_search(artist)
        return catalogue.get(artist, [])[:limit]

    bridge.list_artists = lambda limit=500, query="": list(
        LIBRARY_ARTISTS if artists is None else artists)
    bridge.search_music = _search
    bridge.instant_mix = lambda item_id, limit=10: list(
        (mixes or {}).get(item_id, []))
    bridge.list_playlists = lambda: [
        MediaItem(item_id=f"p{n}", title=t, media_type="playlist")
        for n, t in enumerate(playlists)
    ]

    def _create(name, item_ids=None):
        bridge.calls.append(("create", name, list(item_ids or [])))
        return create if create is not None else "pl-new"

    def _add(name_or_id, item_ids):
        bridge.calls.append(("add", name_or_id, list(item_ids)))
        return "ok"

    bridge.create_playlist = _create
    bridge.playlist_add = _add
    return bridge


def test_build_taste_playlist_matches_and_reports_the_misses():
    bridge = make_bridge()
    out = bridge.build_taste_playlist(
        "Road Trip", ["Coldplay", "Glass Animals", "Chappell Roan"],
        shuffle=False)
    assert out["ok"] is True
    assert out["matched"] == ["Coldplay", "Glass Animals"]
    assert out["missing"] == ["Chappell Roan"]
    assert out["track_count"] == 3
    assert out["playlist_id"] == "pl-new"


def test_build_taste_playlist_fuzzy_matches_but_refuses_a_weak_one():
    bridge = make_bridge()
    out = bridge.build_taste_playlist("Mix", ["coldpaly", "Cold War Kids"],
                                      shuffle=False)
    assert out["matched"] == ["Coldplay"]
    assert out["missing"] == ["Cold War Kids"]


def test_build_taste_playlist_adds_similar_tracks_but_not_the_same_artists():
    mixes = {"a1": [_track("c2", "Coldplay"),          # already included
                    _track("x1", "Keane"),
                    _track("g1", "Glass Animals")]}    # a claimed artist
    bridge = make_bridge(mixes=mixes)
    out = bridge.build_taste_playlist("Mix", ["Coldplay", "Glass Animals"],
                                      shuffle=False)
    assert out["similar_added"] == 1
    ids = bridge.calls[0][2]
    assert ids.count("c2") == 1 and "x1" in ids


def test_build_taste_playlist_interleaves_so_one_artist_cannot_clump():
    tracks = {"Coldplay": [_track(f"c{n}", "Coldplay") for n in range(5)],
              "Wilco": [_track("w1", "Wilco")]}
    bridge = make_bridge(tracks=tracks)
    out = bridge.build_taste_playlist("Mix", ["Coldplay", "Wilco"],
                                      max_tracks=2, shuffle=False)
    assert out["track_count"] == 2
    assert bridge.calls[0][2] == ["c0", "w1"]


def test_build_taste_playlist_never_touches_an_existing_playlist_name():
    bridge = make_bridge(playlists=("Road Trip", "Road Trip 2"))
    out = bridge.build_taste_playlist("Road Trip", ["Wilco"], shuffle=False)
    assert out["name"] == "Road Trip 3"
    assert bridge.calls[0][1] == "Road Trip 3"


def test_build_taste_playlist_creates_nothing_when_no_artist_matches():
    bridge = make_bridge()
    out = bridge.build_taste_playlist("Mix", ["Chappell Roan", "Fred again"])
    assert out["ok"] is False
    assert out["missing"] == ["Chappell Roan", "Fred again"]
    assert bridge.calls == []


def test_build_taste_playlist_creates_nothing_for_an_empty_artist_list():
    bridge = make_bridge()
    assert bridge.build_taste_playlist("Mix", [])["ok"] is False
    assert bridge.calls == []


def test_build_taste_playlist_never_raises_on_an_http_error():
    def boom(_artist):
        raise MediaBridgeError("Jellyfin: 500")

    bridge = make_bridge(on_search=boom)
    out = bridge.build_taste_playlist("Mix", ["Coldplay"], shuffle=False)
    assert out["ok"] is False and out["error"]

    bridge = make_bridge()
    bridge.create_playlist = lambda name, item_ids=None: (_ for _ in ()).throw(
        MediaBridgeError("Jellyfin: 403"))
    out = bridge.build_taste_playlist("Mix", ["Coldplay"], shuffle=False)
    assert out["ok"] is False and "403" in out["error"]


def test_build_taste_playlist_chunks_a_long_list():
    tracks = {"Coldplay": [_track(f"c{n}", "Coldplay") for n in range(150)]}
    bridge = make_bridge(tracks=tracks)
    out = bridge.build_taste_playlist("Mix", ["Coldplay"], per_artist=150,
                                      max_tracks=150, shuffle=False)
    assert out["track_count"] == 150
    assert len(bridge.calls[0][2]) == 100          # create carries the first 100
    assert bridge.calls[1][0] == "add" and len(bridge.calls[1][2]) == 50


# ── the uploads the household sent ──────────────────────────────────────────

UPLOADS = [
    {"id": "upload_stations", "filename": "Station Guide.png",
     "title": "Station Guide", "indexed_at": "2026-09-19T14:02:00",
     "store": "knowledge_upload", "word_count": 40},
    {"id": "upload_lasagna", "filename": "Lasagna Recipe.pdf",
     "title": "Lasagna Recipe", "indexed_at": "2026-09-12T09:00:00",
     "store": "ingestion", "word_count": 400},
    {"id": "upload_soccer", "filename": "IMG_4821.jpg",
     "title": "Fall Soccer", "indexed_at": "2026-09-02T09:00:00",
     "store": "knowledge_upload", "word_count": 3},
]

STATIONS_TEXT = (OCR_SAMPLE + "\n3) q Wilco Radio\n4) q Alt-J Radio\n"
                "5) Thumbprint Radio\nShuffle\nMy Stations")


@pytest.fixture
def uploads(monkeypatch):
    """The two upload stores, stubbed at the one function both read."""
    monkeypatch.setattr(document_lookup, "_load_index",
                        lambda: [dict(d) for d in UPLOADS])
    bodies = {"upload_stations": STATIONS_TEXT,
              "upload_lasagna": "Layer the pasta. " * 40,
              "upload_soccer": "practice friday"}
    monkeypatch.setattr(document_lookup, "text_of",
                        lambda entry, limit=6000:
                        bodies.get((entry or {}).get("id"), "")[:limit])
    return UPLOADS


class FakeTool:
    def __init__(self, reply="ok"):
        self.reply = reply
        self.calls = []

    def invoke(self, spec):
        self.calls.append(spec)
        return self.reply


@pytest.fixture
def tools(monkeypatch):
    """api_server's tool handle, with only the three media tools in it."""
    bag = {"media_list_artists": FakeTool("12 artists in your library: ..."),
           "media_playlist_from_artists": FakeTool("Made 'X' with 9 tracks."),
           "media_playlist_from_document": FakeTool("Made 'Y' with 64 tracks.")}
    holder = type("Tools", (), {"tool_dict": bag})()
    monkeypatch.setattr(api_server, "_get_assistant_tools", lambda: holder)
    return bag


# Phrasings the files fastpath answers, one with a typo in the file name.
ACCEPTS = [
    "please look at the staton guide photo i sent you",
    "I uploaded a file called Station Guide, do you have it?",
    "what files do I have",
    "what have I sent you?",
    "can you read the document I just sent you",
    "look at my uploads",
    "did you get the screenshot I shared",
]

DECLINES = [
    "take a photo of the front porch",
    "show me the front door camera",
    "send a message to the kitchen speaker",
    "what's on the radio",
    "play my favorite station",
    "read me a book",
    "how far away is the moon",
    "what's the weather tomorrow",
]


@pytest.mark.parametrize("said", ACCEPTS)
def test_files_fastpath_accepts(said, uploads, tools):
    assert api_server._try_my_files(said) is not None


@pytest.mark.parametrize("said", DECLINES)
def test_files_fastpath_declines(said, uploads, tools):
    assert api_server._try_my_files(said) is None


def test_the_named_file_is_found_through_the_typo(uploads, tools):
    said = api_server._try_my_files(
        "please look at the staton guide photo i sent you")
    assert "Station Guide" in said
    assert "Coldplay" in said                 # it read the file back


def test_can_you_see_it_names_the_file_and_reads_it(uploads, tools):
    said = api_server._try_my_files(
        "I uploaded a file called Station Guide, do you have it?")
    assert said.startswith("Yes — I have Station Guide")
    assert "2026-09-19" in said


def test_a_file_with_almost_no_ocr_text_says_so(uploads, tools):
    said = api_server._try_my_files("look at the fall soccer picture I sent")
    assert "Fall Soccer" in said
    assert "2 words" in said


def test_an_unnamed_reference_uses_the_newest_upload(uploads, tools):
    said = api_server._try_my_files("can you read the document I just sent you")
    assert "Station Guide" in said


def test_a_miss_says_which_files_she_does_have(uploads, tools):
    said = api_server._try_my_files(
        "I sent you a file called Tax Return 2019, can you see it?")
    assert "Tax Return 2019" in said
    assert "Station Guide" in said and "Lasagna Recipe" in said


def test_the_list_question_counts_and_names_the_recent_ones(uploads, tools):
    said = api_server._try_my_files("what files do I have")
    assert "3 files" in said and "Station Guide" in said


def test_no_uploads_at_all_is_answered_honestly(monkeypatch, tools):
    monkeypatch.setattr(document_lookup, "_load_index", lambda: [])
    said = api_server._try_my_files("can you see the file I sent you")
    assert "haven't sent me any files" in said


# ── precedence: the files fastpath outranks the capability read ─────────────

def _dispatcher_order():
    src = inspect.getsource(api_server._try_instant_answer)
    tree = ast.parse(re.sub(r"^ +", "", src, count=1))
    order = []
    for node in ast.walk(tree.body[0]):
        if isinstance(node, ast.Call):
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None)
            if name and name.startswith("_try_"):
                order.append((node.lineno, name))
    order.sort()
    seen = {}
    for index, (_lineno, name) in enumerate(order):
        seen.setdefault(name, index)
    return seen


@pytest.mark.parametrize("later", ["_try_capability_read", "_try_media_play",
                                   "_try_media_library_query",
                                   "_try_document_read"])
def test_files_fastpath_runs_before_the_matchers_that_stole_the_turn(later):
    order = _dispatcher_order()
    assert order["_try_my_files"] < order[later], (
        f"_try_my_files must run before {later}, or 'a file called "
        f"Station Guide' is answered with radio stations."
    )


def test_the_sentence_that_was_stolen_is_answered_by_the_files_fastpath(
        uploads, tools, monkeypatch, capsys):
    """The capability read must never be reached for this turn."""
    def fail(*_a, **_kw):
        raise AssertionError("the capability read was reached")

    monkeypatch.setattr(api_server, "_try_capability_read", fail)
    said = api_server._try_my_files(
        "I uploaded a file called Station Guide, do you have it?")
    assert "Station Guide" in said
    assert "Files fastpath: read" in capsys.readouterr().out


# ── the taste-playlist fastpath ─────────────────────────────────────────────

def test_playlist_from_a_spoken_artist_list(uploads, tools):
    said = api_server._try_taste_playlist(
        "make me a playlist of glass animals, coldplay and alt-j")
    assert said == "Made 'X' with 9 tracks."
    spec = tools["media_playlist_from_artists"].calls[0]
    name, _, artist_text = spec.partition("|")
    assert name == "Glass Animals Mix"
    assert parse_artist_names(artist_text) == ["glass animals", "coldplay",
                                               "alt-j"]


def test_playlist_name_is_taken_from_called_and_is_not_an_artist(uploads,
                                                                 tools):
    api_server._try_taste_playlist(
        "make a playlist called Road Trip from coldplay and wilco")
    spec = tools["media_playlist_from_artists"].calls[0]
    assert spec.split("|")[0] == "Road Trip"
    assert "Road Trip" not in spec.split("|")[1]


def test_playlist_from_the_file_she_sent(uploads, tools):
    said = api_server._try_taste_playlist(
        "make me a playlist from the station guide photo I sent you")
    assert said == "Made 'Y' with 64 tracks."
    assert tools["media_playlist_from_document"].calls == [
        "Station Guide.png|"]


def test_playlist_based_on_that_list_uses_the_newest_upload(uploads, tools):
    api_server._try_taste_playlist(
        "build a playlist based on the artists in that list I sent you")
    assert tools["media_playlist_from_document"].calls[0].startswith(
        "Station Guide.png")


def test_a_playlist_ask_about_a_file_is_not_answered_as_a_file_read(uploads,
                                                                    tools):
    said = api_server._try_my_files(
        "make me a playlist from the station guide photo I sent you")
    assert said == "Made 'Y' with 64 tracks."


@pytest.mark.parametrize("said", [
    "play my road trip playlist",
    "what playlists do I have",
    "list my playlists",
    "play the jazz station",
    "make me a sandwich",
    "create a reminder to call the plumber",
])
def test_taste_playlist_declines(said, uploads, tools):
    assert api_server._try_taste_playlist(said) is None


def test_the_artist_inventory_question(uploads, tools):
    said = api_server._try_taste_playlist("what artists do I have in jellyfin")
    assert said.startswith("12 artists")
    assert tools["media_list_artists"].calls == [""]


# ── registration ────────────────────────────────────────────────────────────

NEW_TOOLS = ("media_list_artists", "media_playlist_from_artists",
             "media_playlist_from_document")


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_tool_is_defined_and_returned_by_create_tools(name):
    """Checked against the source: building the real tool set needs a box."""
    from puffpai import assistant_tools
    src = inspect.getsource(assistant_tools)
    assert f"def {name}(" in src
    assert re.search(rf"^\s+{name},\s*$", src, re.MULTILINE), (
        f"{name} is defined but never returned from create_tools")


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_tool_is_declared_on_the_playlists_capability(name):
    cap = next(c for c in capability_registry.CAPABILITIES
               if c.id == "media.playlists")
    assert name in cap.all_tools


@pytest.mark.parametrize("query", [
    "make me a playlist of these artists",
    "what artists do I have",
    "build a mix that matches my taste",
])
def test_the_playlist_bucket_binds_the_new_tools(query):
    bound = capability_registry.tools_for_query(query, set(NEW_TOOLS))
    assert set(NEW_TOOLS) & set(bound)


# ═════════════════════════════════════════════════════════════════════════════
#  REVIEW ROUND
# ═════════════════════════════════════════════════════════════════════════════
#
#  F2  a household LIST is not an upload
#  F3  never substitute the newest upload for a file the sentence describes
#  F6  a half-saved playlist is reported as half-saved
#  F7  "never raises" has to be true of a junk Jellyfin shape too

@pytest.fixture
def instant(monkeypatch):
    """`_try_instant_answer` with every rule that is not ours stubbed out.

    The negatives below run through the WHOLE dispatcher, because the bug was
    about which rule claimed the turn — checking `_try_my_files` alone would
    have proved nothing about the live sentence.
    """
    src = inspect.getsource(api_server._try_instant_answer)
    ours = {"_try_taste_playlist", "_try_my_files"}
    for name in set(re.findall(r"\b(_try_\w+)\(", src)):
        if name not in ours and hasattr(api_server, name):
            monkeypatch.setattr(api_server, name, lambda *a, **k: None)
    return api_server._try_instant_answer


LIST_TURNS = [
    "create a list called chores",
    "start a list called party supplies",
    "make a new list called weekend projects",
    "what's on the list called chores",
    "add eggs to the list called groceries",
]


@pytest.mark.parametrize("said", LIST_TURNS)
def test_a_household_list_is_never_answered_from_the_uploads(said, uploads,
                                                             tools, instant):
    """Live shape: all five came back "I don't have anything called 'chores'"."""
    assert instant(said) is None


@pytest.mark.parametrize("said", LIST_TURNS)
def test_the_files_fastpath_stands_down_for_a_list(said, uploads, tools):
    assert api_server._try_my_files(said) is None


def test_a_list_next_to_a_sent_cue_is_still_an_upload(uploads, tools):
    said = api_server._try_my_files("read me that list I sent you")
    assert "Station Guide" in said


@pytest.mark.parametrize("said", [
    "make a shopping list from the recipe I sent you",
    "I uploaded a picture of my medication",
])
def test_a_described_file_is_never_swapped_for_the_newest_upload(said, uploads,
                                                                 tools,
                                                                 instant):
    """Both answered "Yes - I have Station Guide..." before the fix."""
    reply = instant(said)
    assert reply is None or "Yes — I have Station Guide" not in reply


def test_the_shopping_list_turn_belongs_to_the_shopping_list(uploads, tools):
    said = "make a shopping list from the recipe I sent you"
    assert api_server._try_my_files(said) is None
    assert api_server._try_taste_playlist(said) is None


def test_a_descriptor_that_matches_nothing_is_said_out_loud(uploads, tools):
    said = api_server._try_my_files("I uploaded a picture of my medication")
    assert "medication" in said
    assert "Station Guide" in said          # ...as one of the recent titles
    assert "Yes — I have" not in said


def test_a_descriptor_is_resolved_against_the_text_of_recent_uploads(uploads,
                                                                     tools):
    """"the screenshot of coldplay" — the word is inside the FILE."""
    said = api_server._try_my_files(
        "can you see the screenshot of coldplay I sent you")
    assert "Station Guide" in said


def test_a_generic_reference_still_means_the_newest_upload(uploads, tools):
    for said in ("can you read the file I just sent you",
                 "I sent you a document, can you see it?"):
        assert "Station Guide" in api_server._try_my_files(said)


def test_a_seeded_fixture_is_never_the_newest_thing_you_sent(monkeypatch,
                                                             tools):
    rows = [{"id": "fx", "filename": "patient_record.txt",
             "title": "Patient Record", "indexed_at": "2026-09-20T10:00:00",
             "classification": "fixture", "word_count": 120},
            {"id": "real", "filename": "Station Guide.png",
             "title": "Station Guide", "indexed_at": "2026-09-19T14:02:00",
             "classification": "unknown", "word_count": 40}]
    monkeypatch.setattr(document_lookup, "_load_index",
                        lambda: [dict(d) for d in rows])
    monkeypatch.setattr(document_lookup, "text_of",
                        lambda entry, limit=6000: "coldplay radio glass animals")
    assert api_server._files_recent()[0]["id"] == "real"
    assert "Station Guide" in api_server._try_my_files(
        "can you read the file I just sent you")


# ── a failed build says so, in English ───────────────────────────────

def test_a_failed_playlist_build_is_logged_and_said_plainly(uploads, tools,
                                                            capsys):
    class Boom:
        def invoke(self, spec):
            raise RuntimeError("jellyfin down")

    tools["media_playlist_from_artists"] = Boom()
    said = api_server._try_taste_playlist(
        "make me a playlist of coldplay and wilco")
    assert said is not None and "RuntimeError" not in said
    assert "couldn't build that playlist" in said
    assert "[Media] taste playlist -> FAILED: RuntimeError" in capsys.readouterr().out


def _returns_naming_an_exception_class(module):
    """Every spoken RETURN in a fastpath/tool that carries a class name."""
    tree = ast.parse(inspect.getsource(module))
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        if not (fn.name.startswith("_try_") or fn.name.startswith("tool_")):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and node.value is not None:
                text = ast.unparse(node.value)
                if "__name__" in text:
                    found.append(f"{module.__name__}.{fn.name}: {text}")
    return found


def test_no_spoken_reply_names_a_python_exception_class():
    """The class name goes in the [Media] log line, never in a sentence.

    "I hit a snag starting that — RuntimeError" is not something to say to a
    household.
    """
    assert _returns_naming_an_exception_class(api_server) == []


# ── a half-saved playlist ───────────────────────────────────────────────

def test_a_failed_later_chunk_reports_the_playlist_that_exists():
    tracks = {"Coldplay": [_track(f"c{n}", "Coldplay") for n in range(150)]}
    bridge = make_bridge(tracks=tracks)

    def _add(playlist_id, item_ids):
        raise MediaBridgeError("Jellyfin: 504")

    bridge.playlist_add = _add
    out = bridge.build_taste_playlist("Mix", ["Coldplay"], per_artist=150,
                                      max_tracks=150, shuffle=False)
    assert out["ok"] is True and out["partial"] is True
    assert out["playlist_id"] == "pl-new"
    assert out["track_count"] == 100
    assert "504" in out["error"]


def test_the_partial_playlist_is_said_plainly():
    from puffpai import assistant_tools
    body = inspect.getsource(assistant_tools).split(
        "def _taste_playlist_reply(")[1].split("@tool")[0]
    assert "partial" in body
    assert "the first" in body and "the rest didn't save" in body


def test_a_whole_playlist_is_still_reported_whole():
    bridge = make_bridge()
    out = bridge.build_taste_playlist("Mix", ["Coldplay"], shuffle=False)
    assert out["ok"] is True and out["partial"] is False


# ── junk from Jellyfin is an empty result, never an exception ───────────

def _bridge_with_response(payload):
    bridge = object.__new__(MediaBridge)
    bridge._jf_request = lambda path, **kw: payload
    bridge._get_user_id = lambda: "u1"
    return bridge


@pytest.mark.parametrize("payload", [
    [],                                   # a JSON list, not an object
    {"Items": ["not-a-dict", 7, None]},   # rows that are not objects
    {"Items": None},
    "nonsense",
])
def test_instant_mix_and_list_artists_survive_a_junk_shape(payload):
    bridge = _bridge_with_response(payload)
    assert bridge.instant_mix("a1") == []
    assert bridge.list_artists() == []


def test_build_taste_playlist_survives_an_instant_mix_that_raises():
    bridge = make_bridge()

    def boom(item_id, limit=10):
        raise RuntimeError("Jellyfin returned a list")

    bridge.instant_mix = boom
    out = bridge.build_taste_playlist("Mix", ["Coldplay"], shuffle=False)
    assert out["ok"] is True and out["similar_added"] == 0


def test_build_taste_playlist_survives_a_junk_artist_library():
    bridge = make_bridge(artists=["not-a-dict", {"name": ""}, None])
    out = bridge.build_taste_playlist("Mix", ["Coldplay"], shuffle=False)
    assert out["ok"] is False and out["missing"] == ["Coldplay"]
