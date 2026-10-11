"""Offline directions from the box's own map engines.

Puff answers "directions to X", "how do I get to X", "how far is X" and
"nearest X" (drive, walk, bike) from two engines running on this box:

* **Photon** (place search, OpenStreetMap data cut to Texas), biased to the
  household's home point;
* **Valhalla** (routing), from the home point to the place found.

Nothing here reaches past loopback. Both engine URLs are config values
(``maps`` section) and :func:`_engine_url` refuses any host that is not this
box; the calls themselves go through :mod:`puffpai.security.lan_http`, which
refuses any address outside the house before a socket opens. No Bridge
connection is involved.

THE HOME POINT NEVER LEAVES THIS MODULE'S STACK FRAMES. It is sent to the two
loopback engines and nowhere else: never logged (failures log the exception
TYPE only, because a requests error quotes the URL and Photon's URL carries
the bias point), never stored in a route record (the origin is the label
"Home"), never put in a reply, and never put in the phone link (the Organic
Maps link has no start point, so the phone starts from where it is).

Answers are honest: a place that is not in the offline map, engines that are
not running, and a place no road reaches are three different sentences, and
none of them is a guess. When this module claims a turn it always answers it,
so the language model never answers a directions question from memory.

Route records (the dashboard's print page and kiosk steps read them) are kept
under ``$PUFFPAI_DATA_DIR/maps/routes/``; see docs/MAPS_ROUTE_API.md.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)

# ── modes ───────────────────────────────────────────────────────────────────

DRIVE, WALK, BIKE = "drive", "walk", "bike"

#: mode -> Valhalla costing.
COSTING = {DRIVE: "auto", WALK: "pedestrian", BIKE: "bicycle"}
#: mode -> the Organic Maps v2 ``mode`` value. The API page documents only
#: ``mode=drive``; ``walk`` and ``bike`` are unproven until a phone scan.
OMAPS_MODE = {DRIVE: "drive", WALK: "walk", BIKE: "bike"}
#: mode -> the words after the time ("about 12 minutes by car").
MODE_WORDS = {DRIVE: "by car", WALK: "on foot", BIKE: "by bike"}

_MODE_ALTS = (
    (WALK, r"walking|walk|on\s+foot|by\s+foot|afoot"),
    (BIKE, r"biking|bike|by\s+bike|by\s+bicycle|on\s+(?:a|my)\s+bike|"
           r"bicycling|bicycle|cycling"),
    (DRIVE, r"driving|drive|by\s+car|in\s+the\s+car|car"),
)
#: Mode words that may END a place phrase ("... by bike"). Bare "drive",
#: "walk" or "car" are not here: "100 Ocean Drive" is an address.
_TAIL_MODES = (r"driving|walking|biking|cycling|bicycling|on\s+foot|by\s+foot|"
               r"by\s+car|in\s+the\s+car|by\s+bike|by\s+bicycle|"
               r"on\s+(?:a|my)\s+bike")


def mode_from_words(text: str):
    """The mode a phrase names, or ``None`` when it names none."""
    low = (text or "").lower()
    for mode, alt in _MODE_ALTS:
        if re.search(r"\b(?:" + alt + r")\b", low):
            return mode
    return None


# ── fixed sentences (no em dashes; people read these) ──────────────────────

NEEDS_HOME = ("I need your home point before I can give directions. Paste your "
              "home point into the Home point card in Settings.")
NOT_FOUND = "I don't have that place in the offline map."
ENGINES_DOWN = ("The offline maps aren't running right now, so I can't give "
                "directions yet.")
NO_ROUTE = ("I found {label}, but I can't find a road route there in the "
            "offline map.")
TRAFFIC_NOTE = "Times assume normal traffic."
SPOKEN_TAIL = " I've put the steps on the screen and in our chat."

#: The kiosk / print page path the dashboard serves.
PRINT_PATH = "/dashboard/directions/{route_id}/print"

# ── config ──────────────────────────────────────────────────────────────────

DEFAULT_VALHALLA_URL = "http://127.0.0.1:8002"
DEFAULT_PHOTON_URL = "http://127.0.0.1:2322"


def maps_root() -> str:
    """Where the map builds live: ``$PUFFPAI_MAPS_ROOT`` when set, else
    ``maps`` under the install's data dir (``$PUFFPAI_DATA_DIR``, else the
    per-user data dir). The build, activate and refresh scripts use the same
    rule, so the API reads the build they made."""
    env = (os.environ.get("PUFFPAI_MAPS_ROOT") or "").strip()
    if env:
        return env
    from puffpai.statedir import resolve_runtime_dir
    return resolve_runtime_dir(os.path.join(_REPO_ROOT, "state", "maps"),
                               env_subdir="maps")


def default_meta_path() -> str:
    """The live build's META.json under :func:`maps_root`."""
    return os.path.join(maps_root(), "current", "META.json")

#: Seconds per call. The engines are on this box and answer a Texas query in
#: well under a second; a probe gets less.
PHOTON_TIMEOUT = 3.0
VALHALLA_TIMEOUT = 5.0
PROBE_TIMEOUT = 1.5
#: The most a whole directions turn may spend waiting on the engines. Past
#: it, the turn says the offline maps aren't running.
TURN_BUDGET = 6.0

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _maps_cfg():
    try:
        from puffpai.config import get_config
        return getattr(get_config(), "maps", None)
    except Exception:  # pragma: no cover - config always loads on box
        return None


def _cfg_value(name: str, default: str) -> str:
    value = getattr(_maps_cfg(), name, None)
    return str(value).strip() if value else default


def _engine_url(name: str, default: str):
    """The configured engine base URL, or ``None`` if it is not loopback.

    The engines live on this box (127.0.0.1) and the home point is sent to
    them, so a config edit pointing either one anywhere else is refused here
    rather than trusted: it reads as "the offline maps aren't running".
    """
    url = _cfg_value(name, default).rstrip("/")
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    if host not in _LOOPBACK_HOSTS:
        logger.warning("maps: %s is not a loopback address; refusing to use it",
                       name)
        return None
    return url


def data_date():
    """``data_date`` from the current map build's META.json, or ``None``."""
    path = _cfg_value("meta_path", "") or default_meta_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    value = meta.get("data_date") if isinstance(meta, dict) else None
    return str(value).strip() or None if value else None


# ── the home point (resolver owned by puffpai.home_point) ──────────────────

def _valid_pair(lat, lon):
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(la) and math.isfinite(lo)):
        return None
    if not (-90 <= la <= 90 and -180 <= lo <= 180) or (la == 0 and lo == 0):
        return None
    return la, lo


def home_point():
    """``(lat, lon)`` of the house, or ``None`` when the owner has not set it.

    Uses :func:`puffpai.household_geo.configured_coordinates` (the owner's home
    point layered over the config file) and deliberately NOT
    ``household_geo.resolve()``: that may geocode the town name, which is a
    guess at where the house is and a request off the box. Without a home
    point it falls back to the same config fields.
    """
    try:
        from puffpai.household_geo import configured_coordinates
    except ImportError:
        configured_coordinates = None
    if configured_coordinates is not None:
        try:
            return configured_coordinates()
        except Exception as exc:
            logger.warning("maps: home point unavailable (%s)",
                           type(exc).__name__)
            return None
    try:
        from puffpai.config import get_config
        cfg = get_config()
    except Exception:  # pragma: no cover
        return None
    return _valid_pair(getattr(cfg, "latitude", None),
                       getattr(cfg, "longitude", None))


# ── HTTP (loopback only; tests replace the two transports) ──────────────────

class EnginesDown(Exception):
    """An engine did not answer. Carries no URL and no coordinates."""


class NoRoute(Exception):
    """Valhalla answered: no road route between the two points."""


_budget_tl = threading.local()


def _start_budget() -> None:
    _budget_tl.deadline = time.monotonic() + TURN_BUDGET


def _timeout(own: float) -> float:
    """``own``, cut to what is left of this turn's budget. Raises
    :class:`EnginesDown` when the budget is spent."""
    deadline = getattr(_budget_tl, "deadline", None)
    if deadline is None:
        return own
    left = deadline - time.monotonic()
    if left <= 0.05:
        logger.info("maps: turn budget spent")
        raise EnginesDown()
    return min(own, left)


def _transport_get(url, **kwargs):
    from puffpai.security.lan_http import lan_get
    return lan_get(url, **kwargs)


def _transport_post(url, **kwargs):
    from puffpai.security.lan_http import lan_post
    return lan_post(url, **kwargs)


#: Valhalla error codes that mean "no route", not "broken engine"
#: (Valhalla API reference, "HTTP status codes and conditions").
_NO_ROUTE_CODES = frozenset({170, 171, 442, 443})


def _photon_search(q: str, near, *, tags=(), limit: int = 10) -> list:
    base = _engine_url("photon_url", DEFAULT_PHOTON_URL)
    if base is None:
        raise EnginesDown()
    params = [("q", q), ("lat", f"{near[0]:.6f}"), ("lon", f"{near[1]:.6f}"),
              ("limit", str(limit)), ("lang", "en")]
    params += [("osm_tag", f"{k}:{v}") for k, v in tags]
    try:
        resp = _transport_get(base + "/api", params=params,
                              timeout=_timeout(PHOTON_TIMEOUT))
        status = int(getattr(resp, "status_code", 0))
        if status != 200:
            raise EnginesDown()
        data = resp.json()
    except EnginesDown:
        logger.info("maps: place search did not answer")
        raise
    except Exception as exc:
        # TYPE only: the message would quote the URL, which carries the bias
        # point (the house).
        logger.info("maps: place search failed (%s)", type(exc).__name__)
        raise EnginesDown() from None
    feats = data.get("features") if isinstance(data, dict) else None
    return [f for f in (feats or []) if isinstance(f, dict)]


def _valhalla_route(origin, dest, mode: str) -> dict:
    base = _engine_url("valhalla_url", DEFAULT_VALHALLA_URL)
    if base is None:
        raise EnginesDown()
    body = {
        "locations": [{"lat": origin[0], "lon": origin[1]},
                      {"lat": dest[0], "lon": dest[1]}],
        "costing": COSTING[mode],
        "units": "miles",
        "language": "en-US",
        "directions_type": "instructions",
    }
    try:
        resp = _transport_post(base + "/route", json=body,
                               timeout=_timeout(VALHALLA_TIMEOUT))
        status = int(getattr(resp, "status_code", 0))
        try:
            data = resp.json()
        except Exception:
            data = {}
    except Exception as exc:
        logger.info("maps: routing failed (%s)", type(exc).__name__)
        raise EnginesDown() from None
    if status == 200 and isinstance(data, dict) and "trip" in data:
        return data["trip"]
    code = data.get("error_code") if isinstance(data, dict) else None
    if 400 <= status < 500 and code in _NO_ROUTE_CODES:
        logger.info("maps: no route (valhalla code %s)", code)
        raise NoRoute()
    logger.info("maps: routing answered %s (code %s)", status, code)
    raise EnginesDown()


def engine_status() -> dict:
    """``{"valhalla": "up"|"down", "photon": "up"|"down"}``. Sends no
    household data: a status page and a fixed one-word search."""
    out = {}
    checks = (
        ("valhalla", "valhalla_url", DEFAULT_VALHALLA_URL, "/status", None),
        ("photon", "photon_url", DEFAULT_PHOTON_URL, "/api",
         {"q": "texas", "limit": "1"}),
    )
    for name, key, default, path, params in checks:
        base = _engine_url(key, default)
        state = "down"
        if base is not None:
            try:
                resp = _transport_get(base + path, params=params,
                                      timeout=PROBE_TIMEOUT)
                if int(getattr(resp, "status_code", 0)) == 200:
                    state = "up"
            except Exception as exc:
                logger.debug("maps: %s probe failed (%s)", name,
                             type(exc).__name__)
        out[name] = state
    return out


def status() -> dict:
    """What ``GET /maps/status`` returns. No coordinates."""
    engines = engine_status()
    return {
        "valhalla": engines["valhalla"],
        "photon": engines["photon"],
        "live": engines["valhalla"] == "up" and engines["photon"] == "up",
        "data_date": data_date(),
        "home_point_set": home_point() is not None,
    }


def probe_capability():
    """``(available, reason)`` for ``capability_probes`` (``maps.directions``)."""
    engines = engine_status()
    if engines["valhalla"] == "up" and engines["photon"] == "up":
        return True, ""
    down = [n.capitalize() for n in ("valhalla", "photon")
            if engines[n] != "up"]
    return False, ("The offline maps aren't running right now ("
                   + " and ".join(down) + " not answering on this box).")


# ── what was asked ──────────────────────────────────────────────────────────

#: "nearest X" categories -> (spoken name, Photon search word, OSM tags).
CATEGORIES = {
    "library": ("library", "library", (("amenity", "library"),)),
    "gas station": ("gas station", "fuel", (("amenity", "fuel"),)),
    "pharmacy": ("pharmacy", "pharmacy", (("amenity", "pharmacy"),)),
    "hospital": ("hospital", "hospital", (("amenity", "hospital"),)),
    "emergency room": ("emergency room", "hospital",
                       (("amenity", "hospital"),)),
    "urgent care": ("urgent care", "urgent care",
                    (("amenity", "clinic"), ("healthcare", "urgent_care"))),
    "grocery store": ("grocery store", "supermarket",
                      (("shop", "supermarket"), ("shop", "grocery"))),
    "park": ("park", "park", (("leisure", "park"),)),
    "post office": ("post office", "post office",
                    (("amenity", "post_office"),)),
    "school": ("school", "school", (("amenity", "school"),)),
    "coffee shop": ("coffee shop", "cafe", (("amenity", "cafe"),)),
    "restaurant": ("restaurant", "restaurant", (("amenity", "restaurant"),)),
    "police station": ("police station", "police", (("amenity", "police"),)),
    "fire station": ("fire station", "fire station",
                     (("amenity", "fire_station"),)),
}
_CATEGORY_WORDS = {
    "library": "library", "libraries": "library", "public library": "library",
    "gas station": "gas station", "gas stations": "gas station",
    "gas": "gas station", "fuel": "gas station",
    "filling station": "gas station", "petrol station": "gas station",
    "pharmacy": "pharmacy", "pharmacies": "pharmacy", "drugstore": "pharmacy",
    "drug store": "pharmacy",
    "hospital": "hospital", "hospitals": "hospital",
    "er": "emergency room", "e r": "emergency room",
    "emergency room": "emergency room",
    "urgent care": "urgent care", "urgent care clinic": "urgent care",
    "walk in clinic": "urgent care", "clinic": "urgent care",
    "grocery store": "grocery store", "grocery stores": "grocery store",
    "grocery": "grocery store", "supermarket": "grocery store",
    "park": "park", "parks": "park",
    "post office": "post office", "post offices": "post office",
    "school": "school", "schools": "school",
    "coffee": "coffee shop", "coffee shop": "coffee shop",
    "coffee place": "coffee shop", "cafe": "coffee shop",
    "café": "coffee shop",
    "restaurant": "restaurant", "restaurants": "restaurant",
    "place to eat": "restaurant",
    "police": "police station", "police station": "police station",
    "police department": "police station",
    "fire station": "fire station", "fire department": "fire station",
    "firehouse": "fire station",
}

#: Brand -> its name as it appears on the map. Searched by name.
BRANDS = {
    "target": "Target", "costco": "Costco", "heb": "H-E-B",
    "walmart": "Walmart", "home depot": "The Home Depot",
    "the home depot": "The Home Depot", "lowes": "Lowe's",
    "walgreens": "Walgreens", "cvs": "CVS", "starbucks": "Starbucks",
    "mcdonalds": "McDonald's", "whataburger": "Whataburger",
    "sams club": "Sam's Club", "kroger": "Kroger", "best buy": "Best Buy",
    "bucees": "Buc-ee's", "chick fil a": "Chick-fil-A",
    "dollar general": "Dollar General", "trader joes": "Trader Joe's",
    "whole foods": "Whole Foods Market",
}

#: Things people ask "how far" about that are not on a road map.
_SKY = frozenset({
    "moon", "sun", "mars", "venus", "jupiter", "saturn", "mercury", "neptune",
    "uranus", "pluto", "star", "stars", "galaxy", "andromeda", "space",
    "outer space", "earth", "horizon", "iss", "international space station",
    "universe", "alpha centauri", "proxima centauri", "black hole",
    "milky way", "comet", "asteroid", "orbit", "planet", "planets",
})
#: Words that mean the "place" is not a place at all.
_NOT_A_PLACE_WORDS = frozenset({
    "sleep", "recipe", "recipes", "game", "level", "boss", "settings",
    "setting", "menu", "page", "screen", "website", "site", "app", "account",
    "inbox", "heaven", "bed", "know", "algorithm", "neighbor",
    "number", "integer", "prime", "relative", "deal", "agreement", "chapter",
    "episode", "song", "album", "movie", "show", "playlist", "channel",
    "lights", "thermostat", "timer", "alarm",
})
#: Things inside a building ("nearest exit", "closest outlet"). Not on a
#: road map, so these fall through to normal answering.
_IN_BUILDING_WORDS = frozenset({
    "exit", "exits", "bathroom", "bathrooms", "restroom", "restrooms",
    "toilet", "toilets", "door", "doors", "outlet", "outlets", "plug",
    "socket", "charger", "chargers", "window", "windows", "stairs",
    "stairwell", "elevator", "elevators", "room", "closet", "fridge",
    "kitchen", "bedroom", "garage", "extinguisher", "fire extinguisher",
    "first aid kit", "light switch", "switch", "tv", "speaker",
})
_IN_BUILDING_RE = re.compile(
    r"\b(?:in|inside)\s+(?:the|this|my|our)\s+(?:house|room|building|home|"
    r"office|store|mall|kitchen|garage)\b")

#: Whole phrases that are not places ("how do I get to the point").
_NOT_A_PLACE_CORE = frozenset({"point", "end", "bottom", "top", "root",
                               "next one", "front of the line", "other side"})
_NOT_A_PLACE_FIRST = frozenset({
    "make", "cook", "bake", "build", "assemble", "install", "use", "fix", "do",
    "play", "set", "change", "reset", "turn", "open", "know", "be", "sleep",
    "it", "that", "this", "there", "here", "home", "me", "you", "them",
    "too", "so", "very", "my", "your", "how", "work",
})
#: Times, distances and app things: never a place, on any phrasing.
_NOT_A_PLACE_WORDS = _NOT_A_PLACE_WORDS | frozenset({
    "christmas", "thanksgiving", "halloween", "easter", "weekend", "birthday",
    "vacation", "holiday", "holidays", "year", "years", "month", "week",
    "today", "tomorrow", "tonight", "midnight", "noon", "marathon", "mile",
    "miles", "5k", "10k", "leaderboard", "cutoff", "deadline", "dashboard",
    "calendar", "vault", "chat", "youtube", "netflix", "email", "time",
})
_HOME_WORDS = frozenset({"home", "my house", "our house", "the house",
                         "here", "us", "me", "my place", "our place"})

#: Positive evidence that X is a place (for the weak phrasings, "how far is
#: X" / "how do I get to X"): the LAST word(s) of X name a kind of place...
_PLACE_NOUNS = frozenset({
    "library", "park", "store", "shop", "school", "hospital", "airport",
    "church", "museum", "zoo", "stadium", "mall", "station", "downtown",
    "beach", "lake", "river", "university", "college", "campus",
    "courthouse", "capitol", "bank", "hotel", "motel", "restaurant", "cafe",
    "diner", "gym", "pool", "theater", "theatre", "cinema", "arena", "market",
    "supermarket", "pharmacy", "clinic", "plaza", "cemetery", "trailhead",
    "campground", "marina", "pier", "dam", "springs", "falls", "canyon",
    "ranch", "winery", "brewery", "bakery", "depot", "terminal", "dmv",
    "mission", "fairgrounds", "racetrack", "speedway", "outlets",
})
_PLACE_PHRASES = frozenset({
    "community center", "shopping center", "medical center",
    "convention center", "visitor center", "recreation center", "city hall",
    "town square", "rest area", "truck stop", "high school", "middle school",
    "elementary school", "state park", "bus station", "train station",
    "outlet mall", "farmers market", "dog park", "golf course",
})
#: ...or X is a street address...
_STREET_TYPES = frozenset({
    "street", "avenue", "road", "drive", "boulevard", "highway", "freeway",
    "parkway", "lane", "court", "way", "loop", "trail", "circle",
    "expressway",
})
_CITY_PREFIXES = frozenset({"downtown", "north", "south", "east", "west",
                            "central", "old", "greater"})

_DATA_DIR = os.path.dirname(os.path.abspath(__file__))
_TX_PLACES_FILE = os.path.join(_DATA_DIR, "maps_texas_places.txt")
_TX_AMBIGUOUS_FILE = os.path.join(_DATA_DIR, "maps_texas_places_ambiguous.txt")
_name_lists: dict = {}


def _name_list(path: str) -> frozenset:
    if path not in _name_lists:
        names = set()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        names.add(_norm(line))
        except OSError:
            logger.warning("maps: %s is missing", os.path.basename(path))
        _name_lists[path] = frozenset(names)
    return _name_lists[path]


def texas_places() -> frozenset:
    """...or a Texas city or town (data file ``maps_texas_places.txt``)."""
    return _name_list(_TX_PLACES_FILE)


def ambiguous_places() -> frozenset:
    """Towns whose name usually means something else (a first name, Paris,
    "fate"): ``maps_texas_places_ambiguous.txt``."""
    return _name_list(_TX_AMBIGUOUS_FILE)


_TX_QUALIFIER = ("tx", "texas")


def town_of(place: str):
    """``(town, ambiguous, qualified)`` when ``place`` names a Texas town on
    the list ("Dallas", "downtown Austin", "Paris, TX"), else ``None``."""
    words = _norm(_strip_article(place)).split()
    qualified = bool(words) and words[-1] in _TX_QUALIFIER
    if qualified:
        words = words[:-1]
    cities = texas_places()
    for i in range(len(words)):
        town = " ".join(words[i:])
        if town in cities and all(w in _CITY_PREFIXES for w in words[:i]):
            return town, town in ambiguous_places(), qualified
    return None


def _without_qualifier(place: str) -> str:
    """"Paris, TX" -> "Paris": the map's own text never carries the state."""
    return re.sub(r"[,\s]+(?:tx|texas)$", "", place.strip()).strip(" ,")


_LEAD_RE = re.compile(
    r"^(?:(?:hey|ok|okay)\s+)?(?:puff(?:pai|\s*pie|\s*pi)?[,\s]+)?"
    r"(?:(?:please|so|um|uh)[,\s]+)*"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
    r"(?:(?:tell|show|give|get|find)\s+me\s+)?")
_TAIL_RE = re.compile(r"(?:[,\s]+(?:please|thanks|thank you))?[\s?.!]*$")

# STRONG phrasings: the words alone say "a place on the map".
_P_DIRECTIONS = re.compile(
    r"^(?:the\s+)?(?:(?P<m>driving|walking|biking|cycling|bike|bicycle|"
    r"walk|car)\s+)?(?:directions|a\s+route|route)\s+(?:to|for\s+getting\s+to)"
    r"\s+(?P<place>.+)$")
_P_MOVE_TO = re.compile(
    r"^how\s+(?:do|can|would|should|could)\s+(?:i|we)\s+"
    r"(?P<verb>drive|walk|bike|cycle)\s+to\s+(?P<place>.+)$")
# WEAK phrasings: also said about things that are not places, so they need
# evidence that X is one.
_P_FROM = re.compile(
    r"^how\s+far\s+(?:away\s+)?(?:are\s+we|am\s+i|is\s+it|is\s+the\s+house|"
    r"is\s+home)\s+from\s+(?P<place>.+)$")
_P_GET_TO = re.compile(
    r"^(?:how\s+(?:do|can|would|should|could)\s+(?:i|we)\s+get\s+to"
    r"|how\s+to\s+get\s+to"
    r"|what(?:'s|\s+is)\s+the\s+(?:best|fastest|quickest)\s+way\s+to\s+get\s+to)"
    r"\s+(?P<place>.+)$")
_P_HOW_FAR = re.compile(
    r"^(?:how\s+far\s+(?:away\s+)?(?:is|are)\s+(?:it\s+(?:to|from\s+here\s+to)"
    r"\s+)?"
    r"|how\s+long\s+(?:does|would|will)\s+it\s+take\s+(?:me\s+|us\s+)?to\s+"
    r"(?:get|drive|walk|bike|cycle)\s+to\s+"
    r"|how\s+long\s+to\s+(?:get|drive|walk|bike|cycle)\s+to\s+)"
    r"(?P<place>.+)$")
# "nearest X": strong with a category or brand, weak otherwise.
_P_NEAREST = re.compile(
    r"^(?:(?:where(?:'s|s|\s+is|\s+are)|what(?:'s|s|\s+is)|which\s+is|"
    r"find|locate)\s+)?(?:the\s+|a\s+)?(?:nearest|closest|next)\s+"
    r"(?P<place>.+)$")
_P_NEAREST_INNER = re.compile(
    r"^(?:the\s+|a\s+)?(?:nearest|closest|next)\s+(.+)$")

_FROM_RE = re.compile(r"\s+from\s+(?P<from>[a-z' ]+)$")
_NEAR_ME_RE = re.compile(
    r"\s+(?:near\s+(?:me|here|us|the\s+house|my\s+house)|nearby|around\s+here"
    r"|close\s+by|to\s+me)$")
_MODE_TAIL_RE = re.compile(
    r"\s+(?:(?:if\s+i(?:'m)?\s+|if\s+we(?:'re)?\s+)?(?:" + _TAIL_MODES
    + r"))$")

_FU_MODES = (r"walking|walk|foot|biking|bike|bicycle|cycling|driving|drive|"
             r"car")
#: A follow-up needs a follow-up phrase: a bare "walk" or "car" is not one.
_FOLLOWUP_RE = re.compile(
    r"^(?:(?:and|what\s+about|how\s+about|ok(?:ay)?\s+what\s+about|"
    r"(?:and\s+)?(?:the\s+)?same\s+place)\s+(?:if\s+(?:i|we)\s+)?"
    r"(?:by\s+|on\s+)?(?P<m1>" + _FU_MODES + r")(?:\s+instead)?"
    r"|(?:by\s+|on\s+)?(?P<m2>" + _FU_MODES + r")\s+instead)$")


def _norm(text: str) -> str:
    """Lower-case letters and digits, single-spaced ("H-E-B" -> "h e b")."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", (text or "").lower()).split())


def _squash(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", (text or "").lower())


#: Street words, both spellings to one: "123 Main St" matches "123 Main
#: Street" and the other way round. Applied to the ask AND the map's text.
_STREET_ABBREV = {
    "st": "street", "ave": "avenue", "av": "avenue", "rd": "road",
    "dr": "drive", "blvd": "boulevard", "hwy": "highway", "fwy": "freeway",
    "pkwy": "parkway", "ln": "lane", "ct": "court",
    "n": "north", "s": "south", "e": "east", "w": "west",
}
_STOP = ("the", "a", "an", "of", "and")


def _address_tokens(text: str) -> list:
    return [_STREET_ABBREV.get(t, t) for t in _norm(text).split()]


def _strip_article(text: str) -> str:
    return re.sub(r"^(?:the|a|an)\s+", "", text).strip()


class Request:
    """One parsed ask. ``place`` is the user's words, cleaned.

    ``needs_lookup`` is True for a weak phrasing whose X has no static place
    evidence: it is claimed only if Photon has a strong name match for X.
    """

    __slots__ = ("place", "mode", "nearest", "needs_lookup", "town")

    def __init__(self, place, mode, nearest, needs_lookup=False, town=False):
        self.place = place
        self.mode = mode
        self.nearest = nearest
        self.needs_lookup = needs_lookup
        self.town = town

    def __repr__(self):  # pragma: no cover - debugging aid
        return (f"Request(place={self.place!r}, mode={self.mode!r}, "
                f"nearest={self.nearest!r}, lookup={self.needs_lookup!r})")


def category_of(place: str):
    """The :data:`CATEGORIES` key a place phrase names, or ``None``."""
    return _CATEGORY_WORDS.get(_norm(_strip_article(place)))


def brand_of(place: str):
    """The :data:`BRANDS` value a place phrase names, or ``None``."""
    key = _norm(_strip_article(place)).replace("h e b", "heb")
    key = key.replace("lowe s", "lowes").replace("sam s", "sams")
    key = key.replace("mcdonald s", "mcdonalds").replace("buc ee s", "bucees")
    key = key.replace("trader joe s", "trader joes")
    return BRANDS.get(key) or BRANDS.get(key.replace(" ", ""))


def _looks_like_place(place: str) -> bool:
    """False for things that are never a place (the blocklists). Applies to
    every phrasing; it is a guard, not evidence."""
    words = _norm(place).split()
    if not words:
        return False
    core = " ".join(w for w in words if w not in ("the", "a", "an"))
    if not core or core in _SKY or core in _HOME_WORDS or core == "light year":
        return False
    # A house number makes it an address ("10 Sun Dr"), not the sky.
    if (any(w in _SKY for w in words) and len(words) <= 3
            and not any(w.isdigit() for w in words)):
        return False
    if category_of(core) or brand_of(core):
        return True
    if core in _IN_BUILDING_WORDS or words[-1] in _IN_BUILDING_WORDS:
        return False
    if core in _NOT_A_PLACE_CORE or words[0] in _NOT_A_PLACE_FIRST:
        return False
    return not any(w in _NOT_A_PLACE_WORDS for w in words)


def place_evidence(place: str) -> bool:
    """Static evidence that ``place`` is somewhere on a map: a category or
    brand, a place noun at the end ("Zilker Park"), a street address ("1100
    Congress Ave", "Main Street"), or a Texas city or town ("Dallas",
    "downtown Austin")."""
    core = _norm(_strip_article(place))
    if not core:
        return False
    if category_of(core) or brand_of(core):
        return True
    toks = [t for t in _address_tokens(core) if t not in ("the", "a", "an")]
    if not toks:
        return False
    if toks[-1] in _PLACE_NOUNS or " ".join(toks[-2:]) in _PLACE_PHRASES:
        return True
    if len(toks) >= 2 and toks[0].isdigit():
        return True
    if len(toks) >= 2 and toks[-1] in _STREET_TYPES:
        return True
    town = town_of(core)
    # An ambiguous town ("Melissa", "Paris") is evidence only with "Texas".
    return bool(town) and (not town[1] or town[2])


def _clean_place(place: str):
    """``(place, mode)`` with trailing mode words and "near me" removed, or
    ``(None, None)`` when the phrase measures from somewhere other than home."""
    place = place.strip(" ,")
    mode = None
    for _ in range(3):
        before = place
        m = _MODE_TAIL_RE.search(place)
        if m:
            mode = mode or mode_from_words(m.group(0))
            place = place[:m.start()].strip(" ,")
        place = _NEAR_ME_RE.sub("", place).strip(" ,")
        f = _FROM_RE.search(place)
        if f:
            if f.group("from").strip() not in _HOME_WORDS:
                return None, None
            place = place[:f.start()].strip(" ,")
        if place == before:
            break
    return place, mode


def parse_request(text: str):
    """A :class:`Request` when ``text`` may be a directions ask, else ``None``.

    Strong phrasings ("directions to X", "how do I drive to X", "nearest X"
    with a category or brand) are claimed outright. Weak phrasings ("how far
    is X", "how do I get to X", "how far are we from X", "how long to get to
    X") need evidence that X is a place: static (:func:`place_evidence`), or
    else ``needs_lookup`` is set and :func:`answer` claims the turn only if
    Photon has a strong name match. Everything the blocklists refuse ("how do
    I get to sleep", "how far is the moon", "nearest bathroom") is ``None``.
    """
    low = " ".join(str(text or "").lower().replace("’", "'").split())
    low = _TAIL_RE.sub("", _LEAD_RE.sub("", low, count=1)).strip()
    if not low:
        return None
    nearest = False
    strong = True
    mode = None
    m = _P_DIRECTIONS.match(low)
    if m:
        mode = mode_from_words(m.group("m") or "")
        place = m.group("place")
    elif _P_MOVE_TO.match(low):
        m = _P_MOVE_TO.match(low)
        mode = {"drive": DRIVE, "walk": WALK}.get(m.group("verb"), BIKE)
        place = m.group("place")
    else:
        m = _P_FROM.match(low) or _P_GET_TO.match(low) or _P_HOW_FAR.match(low)
        if m:
            strong = False
            place = m.group("place")
            verb = re.search(r"\b(drive|walk|bike|cycle)\s+to\b", low)
            if verb:
                mode = {"drive": DRIVE, "walk": WALK}.get(verb.group(1), BIKE)
        else:
            m = _P_NEAREST.match(low)
            if not m:
                return None
            place = m.group("place")
            nearest = True
            strong = False  # unless X is a category or brand, below
    place, tail_mode = _clean_place(place)
    if not place:
        return None
    mode = mode or tail_mode
    inner = _P_NEAREST_INNER.match(place)
    if inner:
        nearest = True
        place = inner.group(1).strip()
        place, inner_mode = _clean_place(place)
        if not place:
            return None
        mode = mode or inner_mode
    if _IN_BUILDING_RE.search(place) or not _looks_like_place(place):
        return None
    if nearest and (category_of(place) or brand_of(place)):
        strong = True
    town = town_of(place)
    if town and town[1] and not town[2] and not strong:
        # "how far is Melissa" / "how far is Paris": a person or somewhere
        # else, not the Texas town, unless the ask says "Texas".
        return None
    needs_lookup = not strong and not place_evidence(place)
    return Request(_without_qualifier(place) if town else place,
                   mode or DRIVE, nearest, needs_lookup, bool(town))


def parse_followup(text: str):
    """The mode a follow-up ("what about walking?", "and by bike?", "walking
    instead") asks for, or ``None``. A bare "walk" or "car" is not one. Only
    meaningful when this conversation has a recent route."""
    low = " ".join(str(text or "").lower().split())
    low = _TAIL_RE.sub("", low).strip()
    m = _FOLLOWUP_RE.match(low)
    if not m:
        return None
    word = m.group("m1") or m.group("m2")
    return WALK if word == "foot" else mode_from_words(word)


# ── finding the place ───────────────────────────────────────────────────────

def _miles_between(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 3958.8 * 2 * math.asin(min(1.0, math.sqrt(h)))


class Place:
    __slots__ = ("name", "street", "housenumber", "city", "lat", "lon",
                 "label", "osm_key", "osm_value", "state")

    def __init__(self, feature):
        props = feature.get("properties") or {}
        coords = (feature.get("geometry") or {}).get("coordinates") or ()
        self.name = str(props.get("name") or "").strip()
        self.street = str(props.get("street") or "").strip()
        self.housenumber = str(props.get("housenumber") or "").strip()
        self.city = str(props.get("city") or "").strip()
        self.lon = float(coords[0])
        self.lat = float(coords[1])
        self.osm_key = str(props.get("osm_key") or "")
        self.osm_value = str(props.get("osm_value") or "")
        self.state = str(props.get("state") or "").strip()
        self.label = self.name
        if self.is_town and self.name:
            # "Paris, Texas is 280 miles away": a town is always said with its
            # state, so a distance is never mistaken for somewhere else.
            self.label = f"{self.name}, {self.state or 'Texas'}"

    @property
    def is_town(self) -> bool:
        return self.osm_key == "place" and self.osm_value in _TOWN_VALUES

    @property
    def address(self) -> str:
        street = " ".join(p for p in (self.housenumber, self.street) if p)
        return ", ".join(p for p in (street, self.city) if p)

    def text(self) -> str:
        return _norm(" ".join((self.name, self.housenumber, self.street,
                               self.city)))

    def address_tokens(self) -> list:
        return _address_tokens(self.text())


_TOWN_VALUES = frozenset({"city", "town", "village", "hamlet",
                          "municipality"})


def _places(features) -> list:
    out = []
    for f in features:
        try:
            out.append(Place(f))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _closest(places, home):
    return min(places, key=lambda p: _miles_between(home, (p.lat, p.lon)))


def _with_street(place: Place, name: str) -> str:
    if place.street:
        return f"the {name} on {place.street}"
    if place.city:
        return f"the {name} in {place.city}"
    return f"the {name}"


def find_place(req: Request, home):
    """The :class:`Place` to route to, or ``None`` (not in the offline map).

    Raises :class:`EnginesDown` when Photon does not answer.
    """
    category = category_of(req.place)
    brand = None if category else brand_of(req.place)
    if category:
        spoken, word, tags = CATEGORIES[category]
        places = [p for p in _places(_photon_search(word, home, tags=tags,
                                                    limit=15))]
        if not places:
            return None
        best = _closest(places, home)
        same = [p for p in places if _squash(p.name) == _squash(best.name)]
        if not best.name:
            best.label = _with_street(best, spoken)
        elif len(same) > 1:
            best.label = _with_street(best, best.name)
        return best
    if brand:
        # Whole words only: "Target" and "H-E-B plus!" match, "Targeted
        # Signs" does not.
        wants = {_norm(brand), _norm(re.sub(r"^The\s+", "", brand))}

        def _is_brand(name):
            n = _norm(name)
            return any(n == w or n.startswith(w + " ") for w in wants)

        places = [p for p in _places(_photon_search(brand, home, limit=15))
                  if _is_brand(p.name)]
        if not places:
            return None
        best = _closest(places, home)
        best.label = _with_street(best, brand)
        return best
    tokens = [t for t in _address_tokens(_strip_article(req.place))
              if t not in ("the", "a", "an", "of", "and")]
    if not tokens:
        return None
    places = [p for p in _places(_photon_search(req.place, home, limit=10))
              if all(t in p.address_tokens() for t in tokens)]
    if req.town and any(p.is_town for p in places):
        places = [p for p in places if p.is_town]
    if not places:
        return None
    best = _closest(places, home)
    same = [p for p in places if _squash(p.name) == _squash(best.name)]
    if not best.name:
        best.label = best.address or req.place
    elif len(same) > 1 and not best.is_town:
        best.label = _with_street(best, best.name)
    return best


def lookup_place(req: Request, home):
    """For a weak phrasing with no static evidence: the closest Photon hit
    whose NAME strongly matches X (at least 0.8 of X's words are in the name
    and X covers at least half the name), or ``None``. ``None`` means the turn
    is not a directions ask after all and falls through."""
    want = [t for t in _address_tokens(_strip_article(req.place))
            if t not in _STOP]
    if not want:
        return None
    strong = []
    for p in _places(_photon_search(_strip_article(req.place), home,
                                    limit=10)):
        name = [t for t in _address_tokens(p.name) if t not in _STOP]
        if not name:
            continue
        shared = len(set(want) & set(name))
        if shared / len(set(want)) >= 0.8 and shared / len(set(name)) >= 0.5:
            strong.append(p)
    if not strong:
        return None
    best = _closest(strong, home)
    same = [p for p in strong if _squash(p.name) == _squash(best.name)]
    if len(same) > 1 and not best.is_town:
        best.label = _with_street(best, best.name)
    return best


# ── words ───────────────────────────────────────────────────────────────────

def miles_words(mi: float) -> str:
    if mi < 0.1:
        return "less than a tenth of a mile"
    if mi < 10:
        value = f"{mi:.1f}".rstrip("0").rstrip(".")
    else:
        value = f"{round(mi):d}"
    return f"{value} mile" if value == "1" else f"{value} miles"


def minutes_words(seconds: float) -> str:
    minutes = max(1, int(round(float(seconds) / 60.0)))
    if minutes < 60:
        return "1 minute" if minutes == 1 else f"{minutes} minutes"
    hours, rest = divmod(minutes, 60)
    h = "1 hour" if hours == 1 else f"{hours} hours"
    if not rest:
        return h
    return h + (" 1 minute" if rest == 1 else f" {rest} minutes")


def _cap_first(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def omaps_url(lat: float, lon: float, name: str, mode: str) -> str:
    """The Organic Maps v2 navigation link with ``origin=currentLocation``
    (documented on omaps.app/api): the phone starts from where it is, so the
    home point never goes in a link or a QR code."""
    return ("https://omaps.app/v2/nav?origin=currentLocation"
            "&destination={:.6f},{:.6f}&destination_name={}&mode={}"
            .format(lat, lon, quote(name or "Destination", safe=""),
                    OMAPS_MODE[mode]))


def _steps(trip: dict) -> list:
    steps = []
    for leg in trip.get("legs") or ():
        for man in leg.get("maneuvers") or ():
            text = str(man.get("instruction") or "").strip()
            if not text:
                continue
            steps.append({
                "n": len(steps) + 1,
                "text": text,
                "distance_mi": round(float(man.get("length") or 0.0), 2),
                "duration_s": int(round(float(man.get("time") or 0.0))),
            })
    return steps


def chat_text(record: dict) -> str:
    """The full answer for chat (and the household thread)."""
    lines = [
        "Directions to {} {}: {}, about {}.".format(
            record["destination"]["label"], MODE_WORDS[record["mode"]],
            miles_words(record["distance_mi"]),
            minutes_words(record["duration_s"])),
        "",
    ]
    for s in record["steps"]:
        tail = f" ({miles_words(s['distance_mi'])})" if s["distance_mi"] else ""
        lines.append(f"{s['n']}. {s['text']}{tail}")
    lines.append("")
    if record.get("traffic_note"):
        lines.append(record["traffic_note"])
    if record.get("data_date"):
        lines.append(f"Map data from {record['data_date']}.")
    else:
        lines.append("I don't know the date of this map data.")
    lines.append(f"Open it on your phone: {record['omaps_url']}")
    lines.append("Printable steps: "
                 + PRINT_PATH.format(route_id=record["id"]))
    return "\n".join(lines)


def spoken_text(record: dict, steps_only: bool = False) -> str:
    """Two sentences: distance and time, then the first turn. The caller
    caps it with the voice budget and adds :data:`SPOKEN_TAIL`."""
    when = minutes_words(record["duration_s"])
    how = MODE_WORDS[record["mode"]]
    if record["mode"] == DRIVE:
        how += " in normal traffic"
    first = _cap_first(record["destination"]["label"])
    sentence1 = "{} is {} away, about {} {}.".format(
        first, miles_words(record["distance_mi"]), when, how)
    turns = [s["text"].rstrip(".") for s in record["steps"][:2]]
    if not turns:
        return sentence1
    if len(turns) == 2 and len(record["steps"]) > 2:
        sentence2 = f"{turns[0]}, then {turns[1][:1].lower()}{turns[1][1:]}."
    else:
        sentence2 = f"{turns[0]}."
    return f"{sentence1} {sentence2}"


# ── route records ───────────────────────────────────────────────────────────

KEEP_ROUTES = 50
_ID_RE = re.compile(r"[0-9a-f]{16}")
_store_lock = threading.Lock()
_ROUTES_DIR_OVERRIDE: str | None = None

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def routes_dir() -> str:
    """``$PUFFPAI_DATA_DIR/maps/routes``, else ``maps/routes`` under the
    per-user data dir. Same
    convention as ``home_point.overlay_path``: the env root plus a subdir."""
    if _ROUTES_DIR_OVERRIDE:
        return _ROUTES_DIR_OVERRIDE
    from puffpai.statedir import resolve_runtime_dir
    return resolve_runtime_dir(
        os.path.join(_REPO_ROOT, "state", "maps", "routes"),
        env_subdir=os.path.join("maps", "routes"))


def valid_route_id(route_id) -> bool:
    return isinstance(route_id, str) and bool(_ID_RE.fullmatch(route_id))


def save_route(record: dict) -> None:
    """Write one record atomically, then keep only the newest 50."""
    folder = routes_dir()
    with _store_lock:
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".route.", suffix=".tmp",
                                   dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, os.path.join(folder, record["id"] + ".json"))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _prune_locked(folder)


def _records_newest_first(folder: str) -> list:
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext != ".json" or not valid_route_id(stem):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                created = str(json.load(fh).get("created_at") or "")
        except (OSError, ValueError, AttributeError):
            created = ""
        out.append((created, path))
    out.sort(reverse=True)
    return out


def _prune_locked(folder: str) -> None:
    for _created, path in _records_newest_first(folder)[KEEP_ROUTES:]:
        try:
            os.remove(path)
        except OSError:
            pass


def load_route(route_id):
    """The record for ``route_id`` or ``None``. ``route_id`` is validated
    strictly (16 lower-case hex digits) before it touches a path."""
    if not valid_route_id(route_id):
        return None
    try:
        with open(os.path.join(routes_dir(), route_id + ".json"), "r",
                  encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def latest_route():
    with _store_lock:
        newest = _records_newest_first(routes_dir())[:1]
    if not newest:
        return None
    return load_route(os.path.splitext(os.path.basename(newest[0][1]))[0])


# ── the kiosk ───────────────────────────────────────────────────────────────

def show_on_kiosk(record: dict, source: str = "voice") -> None:
    """Push a ``directions`` item to the show-me viewer (/dashboard/display)."""
    try:
        from puffpai import dashboard_display
        dashboard_display.set_display(
            dashboard_display.KIND_DIRECTIONS,
            PRINT_PATH.format(route_id=record["id"]),
            title="Directions to " + record["destination"]["label"],
            source=source,
            meta={"route_id": record["id"], "mode": record["mode"]})
    except Exception as exc:  # the answer stands without the screen
        logger.warning("maps: could not show directions on the kiosk (%s)",
                       type(exc).__name__)


# ── the turn ────────────────────────────────────────────────────────────────

#: conversation id -> (monotonic, destination dict). In memory only.
_LAST: dict = {}
_LAST_TTL = 10 * 60
_last_lock = threading.Lock()

#: The spoken form of the answer this thread just produced, keyed by its chat
#: text, so the voice pipeline can speak two sentences while the transcript
#: and the household thread keep every step.
_spoken_tl = threading.local()


def spoken_for(chat: str):
    """Pop the spoken form of ``chat`` if this thread just produced it
    (``chat`` holds the full answer, possibly with a notice in front)."""
    pair = getattr(_spoken_tl, "pair", None)
    _spoken_tl.pair = None
    # Contained, not equal: a surface may prepend a notice to the reply
    # (the pending-PIN mention, the PII honesty line).
    if pair and isinstance(chat, str) and pair[0] in chat:
        return pair[1]
    return None


def _remember(conversation_id, destination) -> None:
    if not conversation_id:
        return
    with _last_lock:
        now = time.monotonic()
        for key in [k for k, v in _LAST.items() if now - v[0] > _LAST_TTL]:
            _LAST.pop(key, None)
        _LAST[str(conversation_id)] = (now, dict(destination))


def _last_destination(conversation_id):
    if not conversation_id:
        return None
    with _last_lock:
        hit = _LAST.get(str(conversation_id))
        if not hit or time.monotonic() - hit[0] > _LAST_TTL:
            return None
        return dict(hit[1])


def reset() -> None:
    """Forget follow-up memory (tests)."""
    with _last_lock:
        _LAST.clear()
    _spoken_tl.pair = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _route_to(destination: dict, mode: str, home, *, conversation_id=None,
              source="voice") -> str:
    """Route home -> destination, store, show, and return the chat text."""
    dest_point = (destination["lat"], destination["lon"])
    try:
        trip = _valhalla_route(home, dest_point, mode)
    except NoRoute:
        return NO_ROUTE.format(label=destination["label"])
    summary = trip.get("summary") or {}
    route_id = secrets.token_hex(8)
    record = {
        "id": route_id,
        "created_at": _now_iso(),
        "mode": mode,
        "origin": {"label": "Home"},
        "destination": {
            "label": destination["label"],
            "address": destination.get("address") or "",
            "lat": round(destination["lat"], 6),
            "lon": round(destination["lon"], 6),
        },
        "distance_mi": round(float(summary.get("length") or 0.0), 2),
        "duration_s": int(round(float(summary.get("time") or 0.0))),
        "steps": _steps(trip),
        "data_date": data_date(),
        "traffic_note": TRAFFIC_NOTE if mode == DRIVE else None,
        "omaps_url": omaps_url(destination["lat"], destination["lon"],
                               destination.get("name")
                               or destination["label"], mode),
    }
    try:
        save_route(record)
    except OSError as exc:
        logger.warning("maps: could not store the route (%s)",
                       type(exc).__name__)
    show_on_kiosk(record, source=source)
    _remember(conversation_id, record["destination"]
              | {"name": destination.get("name") or ""})
    chat = chat_text(record)
    _spoken_tl.pair = (chat, spoken_text(record))
    logger.info("maps: answered a %s route (%s steps)", mode,
                len(record["steps"]))
    return chat


def answer(text: str, conversation_id=None, source: str = "voice"):
    """The reply to a directions ask, or ``None`` when ``text`` is not one.

    Once this claims a turn it always returns a sentence: steps, or one of
    the honest misses. It never returns ``None`` for a claimed turn, so the
    language model never answers a directions question from memory. A weak
    phrasing with no static evidence is claimed only when Photon finds a
    strong name match; without a home point or with Photon down it is NOT
    claimed (so "how do I get to my calendar" never asks for a home point).
    """
    _spoken_tl.pair = None
    _start_budget()
    req = parse_request(text)
    followup_mode = None
    if req is None:
        followup_mode = parse_followup(text)
        if followup_mode is None:
            return None
        last = _last_destination(conversation_id)
        if last is None:
            return None
    home = home_point()
    if req is not None and req.needs_lookup:
        if home is None:
            return None
        try:
            place = lookup_place(req, home)
        except EnginesDown:
            return None
        if place is None:
            return None
        destination = {"label": place.label or req.place,
                       "name": place.name, "address": place.address,
                       "lat": place.lat, "lon": place.lon}
        try:
            return _route_to(destination, req.mode, home,
                             conversation_id=conversation_id, source=source)
        except EnginesDown:
            return ENGINES_DOWN
    if home is None:
        return NEEDS_HOME
    try:
        if req is None:
            return _route_to(last, followup_mode, home,
                             conversation_id=conversation_id, source=source)
        place = find_place(req, home)
        if place is None:
            return NOT_FOUND
        destination = {"label": place.label or req.place,
                       "name": place.name, "address": place.address,
                       "lat": place.lat, "lon": place.lon}
        return _route_to(destination, req.mode, home,
                         conversation_id=conversation_id, source=source)
    except EnginesDown:
        return ENGINES_DOWN
