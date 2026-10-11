"""Offline directions by voice and chat.

Fixtures are hand-written in the documented shapes (Photon GeoJSON, Valhalla
``/route`` trip JSON); the two transports are replaced, so no engine runs and
nothing dials. The home point is SYNTHETIC (no real address), and its digits
are hunted for in every log, record, reply, link and screen item.

Run: python -m pytest -q -p no:cacheprovider tests/test_improve_maps_directions.py
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from puffpai import maps_directions as md  # noqa: E402

#: Synthetic home point. Its digits must never appear outside the engines.
HOME = (31.123456, -99.654321)
HOME_MARKS = ("31.1234", "99.6543")


# ── hand-written engine fixtures ────────────────────────────────────────────

def _feature(name, lat, lon, street="", housenumber="", city="Testville",
             key="amenity", value="library"):
    props = {"name": name, "street": street, "housenumber": housenumber,
             "city": city, "osm_key": key, "osm_value": value,
             "country": "United States", "state": "Texas"}
    return {"type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {k: v for k, v in props.items() if v}}


LIBRARIES = [
    _feature("Central Library", 31.40, -99.60, street="Oak Avenue"),
    _feature("Oak Street Library", 31.15, -99.65, street="Main Street",
             housenumber="600"),
]
TARGETS = [
    _feature("Target", 31.30, -99.70, street="Loop 100", key="shop",
             value="department_store"),
    _feature("Target", 31.14, -99.66, street="Oakmont Road", key="shop",
             value="department_store"),
    _feature("Targeted Signs", 31.13, -99.65, street="Elm Street",
             key="shop", value="signs"),
]
NAMED = [
    _feature("Joe's Pizza", 31.20, -99.70, street="Pine Street",
             key="amenity", value="restaurant"),
    _feature("Pizza Palace", 31.16, -99.66, street="Elm Street",
             key="amenity", value="restaurant"),
]


def _trip(length=4.2, time_s=720):
    return {"trip": {
        "locations": [{"lat": HOME[0], "lon": HOME[1], "type": "break"},
                      {"lat": 31.15, "lon": -99.65, "type": "break"}],
        "summary": {"length": length, "time": time_s},
        "units": "miles", "language": "en-US", "status": 0,
        "legs": [{"summary": {"length": length, "time": time_s},
                  "maneuvers": [
                      {"type": 2, "instruction": "Drive north on Elm Street.",
                       "verbal_pre_transition_instruction":
                           "Drive north on Elm Street.",
                       "length": 0.5, "time": 60},
                      {"type": 10, "instruction": "Turn right onto Main Street.",
                       "verbal_pre_transition_instruction":
                           "Turn right onto Main Street.",
                       "length": 3.6, "time": 600},
                      {"type": 15, "instruction": "Turn left onto Library Lane.",
                       "length": 0.1, "time": 60},
                      {"type": 5, "instruction":
                           "Your destination is on the right.",
                       "length": 0.0, "time": 0},
                  ]}]}}


NO_PATH = {"error_code": 442, "error": "No path could be found for input",
           "status_code": 400, "status": "Bad Request"}


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class Engines:
    """The fake HTTP layer: records every call, answers from fixtures."""

    def __init__(self):
        self.calls = []
        self.photon = {"library": LIBRARIES, "target": TARGETS}
        self.photon_default = NAMED
        self.route = (200, _trip())
        self.down = False

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append(("GET", url, params))
        if self.down:
            # A real requests error quotes the URL, which carries the point.
            raise ConnectionError(f"refused: {url}?{params}")
        path = urlsplit(url).path
        if path == "/status":
            return _Resp(200, {"version": "3.5.0"})
        q = dict(params or [])
        if q.get("q") == "texas":
            return _Resp(200, {"features": []})
        word = str(q.get("q", "")).lower()
        return _Resp(200, {"type": "FeatureCollection",
                           "features": self.photon.get(word,
                                                       self.photon_default)})

    def post(self, url, json=None, timeout=None, **kw):
        self.calls.append(("POST", url, json))
        if self.down:
            raise ConnectionError(f"refused: {url} {json}")
        return _Resp(*self.route)

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


@pytest.fixture
def engines(monkeypatch, tmp_path):
    eng = Engines()
    monkeypatch.setattr(md, "_transport_get", eng.get)
    monkeypatch.setattr(md, "_transport_post", eng.post)
    monkeypatch.setattr(md, "home_point", lambda: HOME)
    meta = tmp_path / "META.json"
    meta.write_text(json.dumps({"data_date": "2026-09-30"}), encoding="utf-8")
    cfg = SimpleNamespace(valhalla_url="http://127.0.0.1:8002",
                          photon_url="http://127.0.0.1:2322",
                          meta_path=str(meta))
    monkeypatch.setattr(md, "_maps_cfg", lambda: cfg)
    monkeypatch.setattr(md, "_ROUTES_DIR_OVERRIDE", str(tmp_path / "routes"))
    from puffpai import dashboard_display as dd
    monkeypatch.setattr(dd, "_STATE_PATH", str(tmp_path / "display.json"))
    md.reset()
    eng.cfg = cfg
    eng.tmp = tmp_path
    yield eng
    md.reset()


@pytest.fixture
def no_engines(monkeypatch):
    """Any engine call fails the test: for phrases that must not be claimed."""
    def boom(*a, **k):
        raise AssertionError("a declined phrase reached the map engines")
    monkeypatch.setattr(md, "_transport_get", boom)
    monkeypatch.setattr(md, "_transport_post", boom)
    monkeypatch.setattr(md, "home_point", lambda: HOME)
    md.reset()


# ── 1. phrase routing ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text,place,mode,nearest", [
    ("directions to the library", "the library", "drive", False),
    ("Hey Puff, how do I get to the library?", "the library", "drive", False),
    ("give me directions to Oak Street Library", "oak street library", "drive",
     False),
    ("how far is Costco", "costco", "drive", False),
    ("how far is Costco from here?", "costco", "drive", False),
    ("how far away is the park", "the park", "drive", False),
    ("where's the nearest gas station", "gas station", "drive", True),
    ("where is the nearest pharmacy", "pharmacy", "drive", True),
    ("nearest hospital", "hospital", "drive", True),
    ("what's the nearest Target", "target", "drive", True),
    ("find the nearest urgent care", "urgent care", "drive", True),
    ("directions to the nearest H-E-B", "h-e-b", "drive", True),
    ("walking directions to the park", "the park", "walk", False),
    ("directions to the park by bike", "the park", "bike", False),
    ("how do I get to the library on foot", "the library", "walk", False),
    ("how do we get to the post office biking", "the post office", "bike",
     False),
    ("how long does it take to walk to the library", "the library", "walk",
     False),
    ("how far is the nearest coffee shop driving", "coffee shop", "drive",
     True),
    ("directions to 100 Ocean Drive", "100 ocean drive", "drive", False),
])
def test_directions_phrases_are_claimed(text, place, mode, nearest):
    req = md.parse_request(text)
    assert req is not None, text
    assert (req.place, req.mode, req.nearest) == (place, mode, nearest)


#: Must fall through to normal answering. Includes existing fastpath phrases
#: owned by other rules (weather, timers, media, Home Assistant, show-me,
#: shopping, clock) and the capability-gap corpus's "nearest gas prices".
DECLINED = [
    "how do I get to sleep",
    "how do i get to sleep faster",
    "directions for this recipe",
    "what are the directions for this recipe",
    "directions to make bread",
    "how far is the moon",
    "how far away is the moon",
    "how far is the moon from earth",
    "how far is the sun",
    "what's the nearest star",
    "how far is Austin from Dallas",
    "how do I get to the settings page",
    "how do I get to the point",
    "how far is it",
    "how far along is my order",
    "what is the nearest neighbor algorithm",
    "make a list of the nearest gas prices",
    "play directions by one direction",
    "play some jazz radio",
    "turn off the kitchen lights",
    "turn on the hallway light",
    "set a timer for 10 minutes",
    "what's the weather",
    "any severe weather alerts",
    "what time is it",
    "add milk to the shopping list",
    "show me the library",
    "stop the music",
    "directions to home",
    # in-building asks: not on a road map
    "where's the nearest exit",
    "where is the nearest bathroom",
    "nearest restroom",
    "where's the closest door",
    "where's the nearest outlet in this room",
    "find the nearest charger",
    "where is the closest bathroom in the house",
    "how do I get to the kitchen",
    "how far is the garage",
]


@pytest.mark.parametrize("text", DECLINED)
def test_non_place_asks_are_not_claimed(text, no_engines):
    assert md.parse_request(text) is None
    assert md.answer(text, conversation_id="home-owner") is None


@pytest.mark.parametrize("text", DECLINED[:6] + ["what about walking?"])
def test_the_api_fastpath_declines_them_too(text, no_engines):
    from puffpai import api_server as api
    assert api._try_maps_directions(text, "home-owner") is None


@pytest.mark.parametrize("words,mode", [
    ("driving", "drive"), ("by car", "drive"), ("walking", "walk"),
    ("on foot", "walk"), ("biking", "bike"), ("by bike", "bike"),
    ("cycling", "bike"), ("", None),
])
def test_mode_words(words, mode):
    assert md.mode_from_words(words) == mode


def test_the_fastpath_sits_after_weather_and_before_timers():
    import inspect
    from puffpai import api_server as api
    src = inspect.getsource(getattr(api._try_instant_answer, "__wrapped__",
                                    api._try_instant_answer))
    assert (src.index("_try_weather_answer(") < src.index("_try_maps_directions(")
            < src.index("_try_timer_set("))


def test_the_fastpath_is_attributed_to_the_maps_row():
    from puffpai.chat_activity import FASTPATH_CAPABILITY
    from puffpai import capability_registry as reg
    assert FASTPATH_CAPABILITY["_try_maps_directions"] == "maps.directions"
    assert reg.get("maps.directions").agent == "toolsmith"


# ── 2. finding the place ────────────────────────────────────────────────────

def test_nearest_category_routes_to_the_closest_by_straight_line(engines):
    reply = md.answer("where's the nearest library")
    assert reply.startswith("Directions to Oak Street Library by car")
    photon = [c for c in engines.calls if c[0] == "GET"][0]
    params = photon[2]
    assert ("osm_tag", "amenity:library") in params
    assert dict(params)["lat"] == "31.123456"  # bias goes to Photon, only
    body = engines.posts()[0][2]
    assert body["costing"] == "auto" and body["units"] == "miles"
    assert body["locations"][1] == {"lat": 31.15, "lon": -99.65}


@pytest.mark.parametrize("key", sorted(md.CATEGORIES))
def test_every_category_has_osm_tags(key):
    spoken, word, tags = md.CATEGORIES[key]
    assert spoken and word and tags
    assert all(":" not in k and v for k, v in tags)


def test_a_brand_picks_the_closest_store_and_names_its_street(engines):
    reply = md.answer("where's the nearest Target")
    assert reply.startswith("Directions to the Target on Oakmont Road by car")
    assert "Targeted Signs" not in reply
    assert engines.posts()[0][2]["locations"][1] == {"lat": 31.14,
                                                     "lon": -99.66}


def test_h_e_b_is_matched_however_it_is_spelled():
    assert md.brand_of("h-e-b") == "H-E-B"
    assert md.brand_of("HEB") == "H-E-B"
    assert md.brand_of("the home depot") == "The Home Depot"
    assert md.brand_of("lowe's") == "Lowe's"


def test_a_named_place_needs_every_word_to_match(engines):
    reply = md.answer("directions to Joe's Pizza")
    assert reply.startswith("Directions to Joe's Pizza by car")


@pytest.mark.parametrize("asked,on_map", [
    ("123 Main St", ("123", "Main Street")),
    ("123 Main Street", ("123", "Main St")),
    ("400 N Loop Rd", ("400", "North Loop Road")),
    ("9 Oak Blvd", ("9", "Oak Boulevard")),
    ("7 Elm Ave", ("7", "Elm Avenue")),
    ("55 Ranch Hwy", ("55", "Ranch Highway")),
    ("2 Pine Ln", ("2", "Pine Lane")),
    ("8 Cedar Ct", ("8", "Cedar Court")),
    ("3 Lake Pkwy", ("3", "Lake Parkway")),
    ("10 Sun Dr", ("10", "Sun Drive")),
    ("1 Gulf Fwy", ("1", "Gulf Freeway")),
])
def test_typed_addresses_match_either_spelling(engines, asked, on_map):
    engines.photon_default = [
        _feature("", 31.16, -99.66, housenumber=on_map[0], street=on_map[1],
                 key="place", value="house")]
    reply = md.answer(f"directions to {asked}")
    assert reply.startswith("Directions to "), reply
    assert md.NOT_FOUND not in reply


def test_equally_good_named_matches_pick_the_closest_and_name_the_street(
        engines):
    engines.photon_default = [
        _feature("Bean Counter Cafe", 31.40, -99.40, street="Far Road"),
        _feature("Bean Counter Cafe", 31.13, -99.66, street="Near Lane"),
    ]
    reply = md.answer("how do I get to Bean Counter Cafe")
    assert reply.startswith("Directions to the Bean Counter Cafe on Near Lane")


# ── 3. answer shapes ────────────────────────────────────────────────────────

def test_chat_answer_has_every_part(engines):
    reply = md.answer("directions to the library")
    lines = reply.splitlines()
    assert lines[0] == ("Directions to Oak Street Library by car: 4.2 miles, "
                        "about 12 minutes.")
    assert "1. Drive north on Elm Street. (0.5 miles)" in lines
    assert "3. Turn left onto Library Lane. (0.1 miles)" in lines
    assert "4. Your destination is on the right." in lines
    assert "Times assume normal traffic." in lines
    assert "Map data from 2026-09-30." in lines
    rec = md.latest_route()
    assert f"Printable steps: /dashboard/directions/{rec['id']}/print" in lines
    assert any(line.startswith("Open it on your phone: "
                               "https://omaps.app/v2/nav?origin=currentLocation&")
               for line in lines)
    assert "google" not in reply.lower()
    assert "\u2014" not in reply  # no em dashes in anything people read


def test_walking_has_no_traffic_note_and_walking_costing(engines):
    reply = md.answer("walking directions to the library")
    assert "on foot" in reply.splitlines()[0]
    assert "traffic" not in reply
    assert engines.posts()[0][2]["costing"] == "pedestrian"
    assert md.latest_route()["traffic_note"] is None


def test_unknown_data_date_is_said(engines):
    os.remove(engines.cfg.meta_path)
    reply = md.answer("directions to the library")
    assert "I don't know the date of this map data." in reply


def test_spoken_form_is_two_sentences_with_the_first_turn(engines):
    chat = md.answer("how do I get to the library")
    spoken = md.spoken_for(chat)
    assert spoken == ("Oak Street Library is 4.2 miles away, about 12 minutes by "
                      "car in normal traffic. Drive north on Elm Street, then "
                      "turn right onto Main Street.")
    assert md.spoken_for(chat) is None  # consumed once


def test_voice_turn_speaks_the_cap_and_points_at_screen_and_chat(engines,
                                                                 monkeypatch):
    from puffpai import api_server as api
    monkeypatch.setattr(api._voice_full_tl, "text", None, raising=False)
    spoken = api._process_voice_query("how do I get to the library",
                                      conversation_id="home-owner")
    assert spoken.endswith("I've put the steps on the screen and in our chat.")
    body = spoken[: -len(md.SPOKEN_TAIL)]
    assert len(api._sentence_spans(body)) <= api.VOICE_MAX_SENTENCES
    assert "normal traffic" in body and "Drive north on Elm Street" in body
    # The transcript (and the household thread) keep every numbered step.
    full = api._voice_full_tl.text
    assert "3. Turn left onto Library Lane." in full
    assert "Map data from 2026-09-30." in full


def test_voice_cap_follows_VOICE_MAX_SENTENCES(engines, monkeypatch):
    from puffpai import api_server as api
    monkeypatch.setattr(api, "VOICE_MAX_SENTENCES", 1)
    spoken = api._process_voice_query("how do I get to the library",
                                      conversation_id="home-owner")
    assert spoken == ("Oak Street Library is 4.2 miles away, about 12 minutes by "
                      "car in normal traffic." + md.SPOKEN_TAIL)


def test_chat_turn_gets_the_full_steps(engines):
    from puffpai import api_server as api
    out = api._process_chat_query("directions to the library",
                                  conversation_id="home-owner")
    assert "3. Turn left onto Library Lane." in out["response"]


# ── 4. honest misses ────────────────────────────────────────────────────────

def test_place_not_in_the_map(engines):
    engines.photon_default = []
    assert md.answer("directions to Nowhere Diner") == md.NOT_FOUND
    assert not engines.posts()


def test_engines_down_is_not_the_same_as_not_found(engines):
    engines.down = True
    reply = md.answer("directions to the library")
    assert reply == md.ENGINES_DOWN
    assert "aren't running" in reply and reply != md.NOT_FOUND


def test_photon_error_status_is_engines_down(engines, monkeypatch):
    monkeypatch.setattr(md, "_transport_get", lambda *a, **k: _Resp(500, {}))
    assert md.answer("directions to the library") == md.ENGINES_DOWN


def test_no_road_route_is_said(engines):
    engines.route = (400, NO_PATH)
    reply = md.answer("directions to the library")
    assert reply == ("I found Oak Street Library, but I can't find a road route "
                     "there in the offline map.")
    assert md.latest_route() is None


def test_valhalla_server_error_is_engines_down(engines):
    engines.route = (500, {"error": "boom"})
    assert md.answer("directions to the library") == md.ENGINES_DOWN


def test_home_point_unset_asks_for_it_and_never_guesses(engines, monkeypatch):
    monkeypatch.setattr(md, "home_point", lambda: None)
    reply = md.answer("directions to the library")
    assert reply == md.NEEDS_HOME
    assert "Paste your home point into the Home point card in Settings" in reply
    assert engines.calls == []


def test_a_non_loopback_engine_is_refused(engines):
    engines.cfg.photon_url = "http://192.168.1.50:2322"
    assert md.answer("directions to the library") == md.ENGINES_DOWN
    assert engines.calls == []


def test_home_point_resolver_never_geocodes(monkeypatch):
    """The resolver uses the owner's point/config only, never a town lookup."""
    import types
    fake = types.ModuleType("puffpai.household_geo")
    fake.configured_coordinates = lambda: None
    fake.resolve = lambda: (_ for _ in ()).throw(AssertionError("geocoded"))
    monkeypatch.setitem(sys.modules, "puffpai.household_geo", fake)
    assert md.home_point() is None
    fake.configured_coordinates = lambda: HOME
    assert md.home_point() == HOME


# ── 5. follow-ups in the same conversation ──────────────────────────────────

def test_what_about_walking_reroutes_the_last_destination(engines):
    md.answer("directions to the library", conversation_id="home-owner")
    reply = md.answer("what about walking?", conversation_id="home-owner")
    assert reply.startswith("Directions to Oak Street Library on foot")
    assert engines.posts()[-1][2]["costing"] == "pedestrian"
    reply = md.answer("and by bike?", conversation_id="home-owner")
    assert engines.posts()[-1][2]["costing"] == "bicycle"
    assert "by bike" in reply.splitlines()[0]


def test_a_follow_up_without_a_route_or_in_another_thread_is_not_claimed(
        engines):
    assert md.answer("what about walking?", conversation_id="home-owner") is None
    md.answer("directions to the library", conversation_id="side-1")
    assert md.answer("what about walking?", conversation_id="home-owner") is None


# ── 6. records ──────────────────────────────────────────────────────────────

def test_record_shape(engines):
    md.answer("directions to the library")
    rec = md.latest_route()
    assert set(rec) == {"id", "created_at", "mode", "origin", "destination",
                        "distance_mi", "duration_s", "steps", "data_date",
                        "traffic_note", "omaps_url"}
    assert md.valid_route_id(rec["id"])
    assert rec["origin"] == {"label": "Home"}
    assert rec["destination"] == {"label": "Oak Street Library",
                                  "address": "600 Main Street, Testville",
                                  "lat": 31.15, "lon": -99.65}
    assert (rec["distance_mi"], rec["duration_s"]) == (4.2, 720)
    assert rec["steps"][1] == {"n": 2, "text": "Turn right onto Main Street.",
                               "distance_mi": 3.6, "duration_s": 600}
    assert rec["data_date"] == "2026-09-30"
    assert rec["traffic_note"] == "Times assume normal traffic."
    path = Path(md.routes_dir()) / (rec["id"] + ".json")
    assert json.loads(path.read_text(encoding="utf-8")) == rec
    assert not [p for p in os.listdir(md.routes_dir()) if p.endswith(".tmp")]


def test_routes_land_under_the_data_dir_not_a_nested_state(monkeypatch,
                                                            tmp_path):
    monkeypatch.setattr(md, "_ROUTES_DIR_OVERRIDE", None)
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    assert Path(md.routes_dir()) == tmp_path / "state" / "maps" / "routes"
    monkeypatch.delenv("PUFFPAI_DATA_DIR")
    from puffpai import platform_paths
    assert Path(md.routes_dir()) == (
        Path(platform_paths.user_data_dir()) / "maps" / "routes")


def test_store_keeps_the_newest_50(engines):
    for i in range(55):
        md.save_route({"id": f"{i:016x}",
                       "created_at": f"2026-10-11T00:00:{i:02d}.000000+00:00"})
    names = sorted(os.listdir(md.routes_dir()))
    assert len(names) == md.KEEP_ROUTES == 50
    assert f"{0:016x}.json" not in names and f"{54:016x}.json" in names
    assert md.latest_route()["id"] == f"{54:016x}"


@pytest.mark.parametrize("bad", ["../../etc/passwd", "ABCDEF0123456789",
                                 "abc", "0123456789abcdef0", "", None,
                                 "0123456789abcdeg", "0123456789abcde/"])
def test_route_ids_are_validated_strictly(engines, bad):
    assert not md.valid_route_id(bad)
    assert md.load_route(bad) is None


# ── 7. the API ──────────────────────────────────────────────────────────────

@pytest.fixture
def client(engines):
    from puffpai import api_server as api
    return api, api.app.test_client(), {"X-API-Key": api._API_KEY}


def test_routes_api_needs_the_key(client):
    _api, c, _h = client
    for path in ("/maps/routes/latest", "/maps/routes/0123456789abcdef",
                 "/maps/status"):
        assert c.get(path, environ_base={"REMOTE_ADDR": "192.168.1.77"}
                     ).status_code == 401, path


def test_routes_api_serves_the_records(client):
    _api, c, h = client
    assert c.get("/maps/routes/latest", headers=h).status_code == 404
    md.answer("directions to the library")
    latest = c.get("/maps/routes/latest", headers=h)
    assert latest.status_code == 200
    rid = latest.get_json()["id"]
    one = c.get(f"/maps/routes/{rid}", headers=h)
    assert one.status_code == 200 and one.get_json()["steps"][0]["n"] == 1
    assert c.get("/maps/routes/0123456789abcdef", headers=h).status_code == 404
    assert c.get("/maps/routes/NOT-AN-ID", headers=h).status_code == 400


def test_status_api(client):
    _api, c, h = client
    got = c.get("/maps/status", headers=h).get_json()
    assert got == {"valhalla": "up", "photon": "up", "live": True,
                   "data_date": "2026-09-30", "home_point_set": True}


def test_capability_status_is_probed(engines):
    from puffpai import capability_probes as cp
    from puffpai import capability_registry as reg
    cap = reg.get("maps.directions")
    cp.reset_cache()
    assert reg.runtime_status(cap) == "live"
    engines.down = True
    cp.reset_cache()
    assert reg.runtime_status(cap) == "offline"
    available, reason = cp.probe("maps.directions")
    assert not available and "aren't running" in reason
    cp.reset_cache()


# ── 8. the phone link ───────────────────────────────────────────────────────

def test_omaps_link_starts_from_the_phone_not_the_house():
    url = md.omaps_url(31.15, -99.65, "the Target on Oakmont Road", "walk")
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "omaps.app",
                                                        "/v2/nav")
    q = parse_qs(parts.query)
    assert set(q) == {"origin", "destination", "destination_name", "mode"}
    assert q["origin"] == ["currentLocation"]
    assert q["destination"] == ["31.150000,-99.650000"]
    assert q["destination_name"] == ["the Target on Oakmont Road"]
    assert q["mode"] == ["walk"]
    for start in ("sll", "saddr", "origin_name"):
        assert start not in q
    assert md.omaps_url(1, 2, "x", "drive").endswith("mode=drive")
    assert md.omaps_url(1, 2, "x", "bike").endswith("mode=bike")
    assert "google" not in url


# ── 9. the kiosk and the one conversation ───────────────────────────────────

def test_the_kiosk_gets_a_directions_item(engines):
    from puffpai import dashboard_display as dd
    md.answer("directions to the library", source="voice")
    rec = md.latest_route()
    cur = dd.get_state()["current"]
    assert cur["kind"] == dd.KIND_DIRECTIONS == "directions"
    assert cur["meta"] == {"route_id": rec["id"], "mode": "drive"}
    assert cur["url"] == f"/dashboard/directions/{rec['id']}/print"
    assert cur["title"] == "Directions to Oak Street Library"


def test_a_voice_directions_turn_lands_whole_in_the_household_thread(
        engines, monkeypatch):
    from puffpai import api_server as api
    from puffpai.voice import service
    saved = []
    before = list(api._chat_history)
    api._chat_history.clear()
    monkeypatch.setattr(api, "_save_conversation",
                        lambda conv: saved.append(json.loads(json.dumps(conv))))
    monkeypatch.setattr(api._voice_full_tl, "text", None, raising=False)
    try:
        body = service.brain_payload(
            [{"role": "user", "content": "how do I get to the library"}],
            truncated=False)
        r = api.app.test_client().post("/api/chat", json=body,
                                       headers={"X-API-Key": api._API_KEY})
        assert r.status_code == 200
        spoken = r.get_json()["message"]["content"]
        assert spoken.endswith(md.SPOKEN_TAIL.strip())
        home = [c for c in api._chat_history if c.get("id") == "home-owner"]
        assert home, "the voice turn did not reach home-owner"
        said = [m for m in home[0]["messages"] if m.get("role") == "assistant"]
        text = said[-1]["content"]
        for step in ("1. Drive north on Elm Street.",
                     "2. Turn right onto Main Street.",
                     "3. Turn left onto Library Lane.",
                     "4. Your destination is on the right."):
            assert step in text
    finally:
        api._chat_history[:] = before


def test_home_assistant_voice_on_v1_speaks_two_sentences(engines,
                                                          monkeypatch):
    """HA Assist posts non-streamed /v1: a voice surface. It hears the short
    form; the household thread and the kiosk get the full steps."""
    from puffpai import api_server as api
    from puffpai import dashboard_display as dd
    before = list(api._chat_history)
    api._chat_history.clear()
    monkeypatch.setattr(api, "_save_conversation", lambda conv: None)
    try:
        r = api.app.test_client().post(
            "/v1/chat/completions",
            json={"model": "puffpai", "messages": [
                {"role": "user", "content": "how do I get to the library"}]},
            headers={"X-API-Key": api._API_KEY,
                     "X-Client": "home-assistant"})
        assert r.status_code == 200
        said = r.get_json()["choices"][0]["message"]["content"]
        assert said == ("Oak Street Library is 4.2 miles away, about 12 minutes "
                        "by car in normal traffic. Drive north on Elm Street, "
                        "then turn right onto Main Street." + md.SPOKEN_TAIL)
        home = [c for c in api._chat_history if c.get("id") == "home-owner"]
        text = [m for m in home[0]["messages"]
                if m.get("role") == "assistant"][-1]["content"]
        assert "3. Turn left onto Library Lane." in text
        assert dd.get_state()["current"]["kind"] == "directions"
    finally:
        api._chat_history[:] = before


def test_dashboard_streamed_v1_still_gets_the_full_steps(engines,
                                                         monkeypatch):
    from puffpai import api_server as api
    monkeypatch.setattr(api, "_save_conversation", lambda conv: None)
    monkeypatch.setattr(api, "_detect_route", lambda *a, **k: "rag")
    r = api.app.test_client().post(
        "/v1/chat/completions",
        json={"model": "puffpai", "stream": True, "client_saves": True,
              "messages": [{"role": "user",
                            "content": "directions to the library"}]},
        headers={"X-API-Key": api._API_KEY})
    body = r.get_data(as_text=True)
    assert "3. Turn left onto Library Lane." in body
    assert "on the screen and in our chat" not in body


def test_dashboard_chat_stream_gets_the_full_steps(engines, monkeypatch):
    from puffpai import api_server as api
    before = list(api._chat_history)
    monkeypatch.setattr(api, "_save_conversation", lambda conv: None)

    def no_model(*a, **k):
        raise AssertionError("the model answered a directions question")
    monkeypatch.setattr(api, "_stream_ollama_tokens", no_model)
    try:
        r = api.app.test_client().post(
            "/chat/stream", json={"message": "directions to the library"},
            headers={"X-API-Key": api._API_KEY})
        body = r.get_data(as_text=True)
        assert "3. Turn left onto Library Lane." in body
    finally:
        api._chat_history[:] = before


# ── 10. the home point stays home ───────────────────────────────────────────

def test_home_coordinates_reach_no_log_record_reply_link_or_screen(
        engines, caplog, capsys):
    from puffpai import dashboard_display as dd
    caplog.set_level(logging.DEBUG)
    for name in ("puffpai.maps_directions", "puffpai"):
        logging.getLogger(name).addHandler(caplog.handler)
    try:
        replies = [md.answer("directions to the library",
                             conversation_id="home-owner"),
                   md.answer("what about walking?",
                             conversation_id="home-owner"),
                   md.answer("where's the nearest Target")]
        engines.down = True
        replies.append(md.answer("directions to the library"))
        engines.down = False
        engines.route = (400, NO_PATH)
        replies.append(md.answer("directions to the library"))
    finally:
        for name in ("puffpai.maps_directions", "puffpai"):
            logging.getLogger(name).removeHandler(caplog.handler)
    out = capsys.readouterr()
    haystacks = {
        "log": caplog.text + out.out + out.err,
        "replies": "\n".join(r or "" for r in replies),
        "records": "\n".join(
            Path(md.routes_dir(), n).read_text(encoding="utf-8")
            for n in os.listdir(md.routes_dir())),
        "screen": json.dumps(dd.get_state()),
        "status": json.dumps(md.status()),
    }
    assert haystacks["records"] and "omaps.app" in haystacks["records"]
    for where, text in haystacks.items():
        for mark in HOME_MARKS:
            assert mark not in text, f"home coordinate in the {where}"
    # ...and the engines did receive it (the point is used, just not leaked).
    assert any(c[0] == "POST" and c[2]["locations"][0]["lat"] == HOME[0]
               for c in engines.calls)


# ── 11. config and egress ───────────────────────────────────────────────────

def test_maps_config_defaults_and_null_section(monkeypatch):
    from puffpai import config
    d = config.MapsConfig()
    assert d.valhalla_url == "http://127.0.0.1:8002"
    assert d.photon_url == "http://127.0.0.1:2322"
    assert d.meta_path == ""
    real = config._load_json

    def fake(path):
        if path.endswith("puffpai_config.json"):
            return {"maps": None}
        return real(path)
    monkeypatch.setattr(config, "_load_json", fake)
    assert config._build_config().maps == config.MapsConfig()


def test_engine_calls_go_through_the_house_local_wrapper():
    text = (ROOT / "src" / "puffpai" / "maps_directions.py").read_text(
        encoding="utf-8")
    assert "lan_get" in text and "lan_post" in text
    assert not re.search(r"(?<![\w.])requests\s*\.", text)
    assert "egress_get" not in text  # no Bridge connection for loopback


# ── 12. positive evidence for the weak phrasings ────────────────────────────

#: Weak phrasings about things that are not places. Never claimed, and with
#: no home point they must never say "I need your home point".
NOT_PLACES = [
    "how far is it to Christmas", "how far is a marathon",
    "how do I get to the dashboard", "how do I get to my calendar",
    "how far is too far", "how do I get to youtube",
    "how do I get to the bottom of this", "how far is a light year",
    "how far is 5k", "how far is it to the weekend",
    "how far is it to the end of the year", "how do I get to work on time",
    "how do I get to the top of the leaderboard",
    "how do I get to the other side", "how do I get to the vault",
    "how do I get to the chat", "how far is the cutoff",
    "how far is it to the end of the movie", "how do I get to level 3",
    "how far is the sun",
]


@pytest.mark.parametrize("text", NOT_PLACES)
def test_no_home_point_never_claims_a_non_place(text, monkeypatch):
    from puffpai import api_server as api

    def boom(*a, **k):
        raise AssertionError("a non-place reached the map engines")
    monkeypatch.setattr(md, "_transport_get", boom)
    monkeypatch.setattr(md, "_transport_post", boom)
    monkeypatch.setattr(md, "home_point", lambda: None)
    md.reset()
    assert md.answer(text, conversation_id="home-owner") is None
    reply = api._try_instant_answer(text, conversation_id="home-owner")
    assert "home point" not in str(reply or "")


@pytest.mark.parametrize("text", NOT_PLACES)
def test_with_a_home_point_a_non_place_is_still_not_claimed(text, engines):
    assert md.answer(text, conversation_id="home-owner") is None
    assert not engines.posts()


@pytest.mark.parametrize("text,place,nearest", [
    ("how far are we from Austin", "austin", False),
    ("how far is the next gas station", "gas station", True),
    ("how far is Dallas", "dallas", False),
    ("how far is downtown Austin", "downtown austin", False),
    ("how do I get to Zilker Park", "zilker park", False),
    ("how far is the airport", "the airport", False),
    ("how do I get to 1100 Congress Ave", "1100 congress ave", False),
    ("how do I drive to the zoo", "the zoo", False),
])
def test_weak_phrasings_with_place_evidence_are_claimed(text, place, nearest):
    req = md.parse_request(text)
    assert req is not None and not req.needs_lookup, text
    assert (req.place, req.nearest) == (place, nearest)


def test_city_list_is_data_and_loads():
    places = md.texas_places()
    assert len(places) >= 300
    assert {"austin", "dallas", "san antonio", "el paso"} <= places
    assert "center" not in places and "temple" not in places


def test_no_home_point_with_evidence_still_asks_for_it(monkeypatch):
    monkeypatch.setattr(md, "home_point", lambda: None)
    assert md.answer("how far are we from Austin") == md.NEEDS_HOME
    assert md.answer("how far is the next gas station") == md.NEEDS_HOME


def test_a_strong_photon_name_match_is_evidence(engines):
    reply = md.answer("how do I get to Joe's Pizza")
    assert reply.startswith("Directions to Joe's Pizza by car")


def test_a_weak_name_match_is_not_evidence(engines):
    engines.photon_default = [
        _feature("Grandview Grandma's Kitchen and Bakery Outlet", 31.2, -99.7,
                 street="Pine Street", value="restaurant")]
    assert md.answer("how do I get to grandma's") is None
    assert not engines.posts()


def test_lookup_with_photon_down_falls_through(engines):
    engines.down = True
    assert md.answer("how do I get to Joe's Pizza") is None
    # ...but a request with evidence says the maps are down.
    assert md.answer("how far is Dallas") == md.ENGINES_DOWN


def test_next_gas_station_routes_to_the_nearest_fuel(engines):
    engines.photon["fuel"] = [
        _feature("Shell", 31.13, -99.66, street="Elm Street", value="fuel"),
        _feature("Valero", 31.5, -99.9, street="Far Road", value="fuel")]
    reply = md.answer("how far is the next gas station")
    assert reply.startswith("Directions to Shell by car")
    gets = [c for c in engines.calls if c[0] == "GET"]
    assert ("osm_tag", "amenity:fuel") in gets[0][2]


# ── 13. follow-ups, ids, time budget ────────────────────────────────────────

@pytest.mark.parametrize("bare", ["walk", "drive", "car", "by bike", "walking",
                                  "on foot"])
def test_a_bare_mode_word_does_not_reroute(engines, bare):
    md.answer("directions to the library", conversation_id="home-owner")
    assert md.answer(bare, conversation_id="home-owner") is None


@pytest.mark.parametrize("phrase,costing", [
    ("what about walking?", "pedestrian"), ("and by bike?", "bicycle"),
    ("walking instead", "pedestrian"), ("how about driving", "auto"),
    ("same place by bike", "bicycle")])
def test_follow_up_phrases_reroute(engines, phrase, costing):
    md.answer("directions to the library", conversation_id="home-owner")
    assert md.answer(phrase, conversation_id="home-owner") is not None
    assert engines.posts()[-1][2]["costing"] == costing


def test_follow_up_window_is_ten_minutes(engines):
    assert md._LAST_TTL == 600
    md.answer("directions to the library", conversation_id="home-owner")
    at, dest = md._LAST["home-owner"]
    md._LAST["home-owner"] = (at - 601, dest)
    assert md.answer("what about walking?", conversation_id="home-owner") is None


def test_route_id_with_a_trailing_newline_is_invalid():
    assert md.valid_route_id("0123456789abcdef")
    assert not md.valid_route_id("0123456789abcdef\n")
    assert md.load_route("0123456789abcdef\n") is None


def test_engine_timeouts_are_short(engines):
    seen = []
    get, post = engines.get, engines.post
    engines_get = lambda url, **k: seen.append(("get", k["timeout"])) or get(url, **k)  # noqa: E731
    engines_post = lambda url, **k: seen.append(("post", k["timeout"])) or post(url, **k)  # noqa: E731
    md._transport_get, md._transport_post = engines_get, engines_post
    try:
        md.answer("directions to the library")
    finally:
        md._transport_get, md._transport_post = get, post
    assert seen[0][0] == "get" and 0 < seen[0][1] <= md.PHOTON_TIMEOUT == 3.0
    assert seen[1][0] == "post" and 0 < seen[1][1] <= md.VALHALLA_TIMEOUT == 5.0
    assert md.TURN_BUDGET == 6.0


def test_a_hung_engine_is_capped_by_the_turn_budget(engines, monkeypatch):
    import time as _time
    monkeypatch.setattr(md, "TURN_BUDGET", 0.3)
    real_get = engines.get

    def slow_get(url, **k):
        _time.sleep(0.35)  # Photon answers, but after the whole budget
        return real_get(url, **k)
    monkeypatch.setattr(md, "_transport_get", slow_get)
    start = _time.monotonic()
    assert md.answer("directions to the library") == md.ENGINES_DOWN
    assert _time.monotonic() - start < 1.0
    assert not engines.posts()  # Valhalla was never asked


# ── 14. ambiguous towns, town labels, one engine wait, packaging ────────────

def _town(name, lat, lon):
    return _feature(name, lat, lon, key="place", value="town", city="")


@pytest.mark.parametrize("text", [
    "how far is Humble", "how far is Melissa", "how far is Alice",
    "how far is Victoria", "how far is Allen", "how far is Paris",
    "how far is Atlanta", "how do I get to Princeton", "how do I get to fate",
])
def test_ambiguous_town_names_fall_through(text, engines):
    assert md.parse_request(text) is None
    assert md.answer(text, conversation_id="home-owner") is None
    assert engines.calls == []


@pytest.mark.parametrize("text,town", [
    ("how far is Melissa, Texas", "Melissa"),
    ("directions to Paris", "Paris"),
    ("how far is Paris TX", "Paris"),
    ("how do I drive to Victoria", "Victoria"),
    ("how far is Austin", "Austin"),
])
def test_town_asks_are_claimed_and_said_with_the_state(text, town, engines):
    engines.photon_default = [
        _feature(f"{town} Junior College", 31.13, -99.66, street="College Rd",
                 value="college"),
        _town(town, 31.6, -99.9),
    ]
    reply = md.answer(text)
    assert reply.startswith(f"Directions to {town}, Texas by car"), reply
    q = dict([c for c in engines.calls if c[0] == "GET"][0][2])
    assert q["q"].lower() == town.lower()  # the qualifier is not searched
    assert md.spoken_for(reply).startswith(f"{town}, Texas is ")
    assert md.latest_route()["destination"]["label"] == f"{town}, Texas"


def test_ambiguous_list_is_inside_the_towns_list():
    amb = md.ambiguous_places()
    assert {"tyler", "paris", "fate", "princeton"} <= amb
    assert "austin" not in amb
    assert amb <= md.texas_places()


@pytest.mark.skipif(
    not (ROOT / ".git").exists()
    or not ((ROOT / "setup.py").exists() or (ROOT / "puffpai.spec").exists()),
    reason="repo checkout only (no .git here; installed tree)")
def test_data_files_ship_with_the_package():
    from importlib import resources
    for name in ("maps_texas_places.txt", "maps_texas_places_ambiguous.txt"):
        f = resources.files("puffpai").joinpath(name)
        assert f.is_file(), name
        assert "austin" in f.read_text(encoding="utf-8") or name.endswith(
            "ambiguous.txt")
    setup_py = (ROOT / "setup.py").read_text(encoding="utf-8")
    assert '"maps_texas_places*.txt"' in setup_py


def test_chat_stream_waits_on_a_hung_engine_once_per_turn(engines,
                                                          monkeypatch):
    """The bypass declines (Photon hung for a lookup); the non-rag branch
    then reaches the instant layer again. The engine is asked once."""
    import time as _time
    from puffpai import api_server as api
    photon_calls = []

    def hung_get(url, **k):
        photon_calls.append(url)
        _time.sleep(0.05)
        raise TimeoutError("read timed out")
    monkeypatch.setattr(md, "_transport_get", hung_get)
    monkeypatch.setattr(api, "_save_conversation", lambda conv: None)
    monkeypatch.setattr(api, "_detect_route", lambda *a, **k: "agent")

    def voice_q(message, history=None, conversation_id=None, **k):
        # What the real _process_voice_query does first: the instant layer.
        assert api._try_maps_directions(message, conversation_id) is None
        return "the normal answer"
    monkeypatch.setattr(api, "_process_voice_query", voice_q)
    before = list(api._chat_history)
    try:
        r = api.app.test_client().post(
            "/chat/stream", json={"message": "how do I get to Joe's Pizza"},
            headers={"X-API-Key": api._API_KEY})
        assert "the normal answer" in r.get_data(as_text=True)
    finally:
        api._chat_history[:] = before
    assert len(photon_calls) == 1
