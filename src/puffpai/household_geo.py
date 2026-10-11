"""Where this house is: ONE resolver for every weather surface.

Why this module exists
----------------------
``config.location`` is a place NAME. If the weather card and the alerts path
each found coordinates their own way, the NWS query could run for one place
while the reply said "No active severe weather alerts for <their city>": a
wrong-place all-clear on a severe-weather path. So:

* ``latitude`` / ``longitude`` are real config fields. The owner's home
  point (``puffpai.home_point``, pasted in Settings, kept only in the box's
  data dir) is layered over ``config/puffpai_config.json`` at load; the
  tracked config ships none. When both are set they are the answer, and
  nothing leaves the house to find out where it is.
* Otherwise the configured place name is looked up ONCE through the
  ``geocode`` egress door, only if the owner opened it, and the result is
  cached for the process. The door stays a separate consent because it sends
  the place name itself to OpenStreetMap; see ``security/egress_doors``.
* Otherwise the answer is honestly "I don't know": ``(None, None)``. The
  weather card and the alerts reply each render that as "coordinates not set",
  never as somebody else's weather.

Which process reads it: the API server. Its config singleton is refreshed
when the owner saves or clears the home point (``config.refresh_home_point``).
The voice service (puffpai-voice) also loads config, once, at start, but it
does not use the point today: voice turns go to the API's ``/api/chat``, which
answers weather from here. If a voice-side consumer is ever added, it must
re-read the overlay (``puffpai.home_point.read``) rather than trust its
start-up config.

The point is never echoed: not in a tool result, a reply, a log line or an
API response. Replies say "your saved home point".

Every surface that needs the house's position (the dashboard weather card,
``auto_weather_forecast``, the deterministic alerts reply, the emergency-prep
alerts tool) calls :func:`household_coordinates` here, so they can never
disagree about where the house is.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

#: The geocode door's single host.
GEOCODE_URL = "https://nominatim.openstreetmap.org/search"

#: After a lookup that FAILED for a reason other than a shut door (no
#: internet, empty result, unreadable payload), don't retry sooner than this.
NEGATIVE_TTL = 600  # seconds

#: What a surface says when the house's position is unknown. One sentence,
#: reused by the card and the alerts reply so the two never phrase it apart.
#: It names the one real control: the Home point card in Settings
#: (``puffpai.home_point``). No build ships a point, so a fresh box says this
#: until the owner pastes one in.
UNSET_MESSAGE = ("I don't know this house's location yet. Paste your home "
                 "point into the Home point card in Settings and I'll use it.")

SOURCE_CONFIG = "config"
SOURCE_GEOCODE = "geocode"

_lock = threading.Lock()
#: ``{"location": <place name>, "coords": (lat, lon) | None, "at": monotonic}``
_cache: dict = {}


def _config():
    # ``from puffpai.config import ...``, never ``from puffpai import config``:
    # the latter reads the ATTRIBUTE on the package, which a sys.modules swap
    # (tests/modpatch.py) can leave pointing at a different module object than
    # ``sys.modules["puffpai.config"]``. That divergence made this function
    # read a different config than the rest of the process.
    from puffpai.config import get_config
    return get_config()


def _coord(value, lo: float, hi: float):
    """A finite float within ``[lo, hi]``, or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f < lo or f > hi:  # NaN or out of range
        return None
    return f


def valid_pair(lat, lon):
    """``(lat, lon)`` as floats when BOTH are usable coordinates, else ``None``.

    A single set half is treated as unset: half a position is not a position,
    and guessing the other half would be exactly the bug this module fixes.
    Exactly ``(0, 0)`` is treated as unset too: it is the placeholder people
    type when they mean "none", and no household lives in the Gulf of Guinea.
    """
    la = _coord(lat, -90.0, 90.0)
    lo = _coord(lon, -180.0, 180.0)
    if la is None or lo is None:
        return None
    if la == 0.0 and lo == 0.0:
        return None
    return la, lo


def household_location() -> str:
    """The configured place name (``config.location``), stripped; may be ''."""
    try:
        return str(_config().location or "").strip()
    except Exception as exc:  # pragma: no cover - config always loads on box
        logger.debug("household-geo: config location unavailable: %s", exc)
        return ""


def configured_coordinates():
    """``(lat, lon)`` from ``config.latitude``/``config.longitude``, or ``None``."""
    try:
        cfg = _config()
    except Exception as exc:  # pragma: no cover
        logger.debug("household-geo: config unavailable: %s", exc)
        return None
    return valid_pair(getattr(cfg, "latitude", None),
                      getattr(cfg, "longitude", None))


def _geocode(location: str):
    """Look ``location`` up through the ``geocode`` door, at most once per
    place name (and no more than once per :data:`NEGATIVE_TTL` after a
    failure). Returns ``(lat, lon)`` or ``None``.

    A SHUT door is not cached: refusing costs no network, and the owner
    flipping the switch on in Settings must take effect on the next ask.

    The lock is held ACROSS the network call on purpose. The weather card,
    the forecast tool and the alerts reply all resolve independently under
    the threaded API server, and a cold cache must still mean one outbound
    request carrying the place name, not one per concurrent caller.
    """
    with _lock:
        hit = _cache.get("location") == location
        if hit and _cache.get("coords") is not None:
            return _cache["coords"]
        if hit and time.monotonic() - _cache.get("at", 0) < NEGATIVE_TTL:
            return None

        from puffpai.security import egress_doors as _doors
        try:
            resp = _doors.egress_get(
                "geocode",
                GEOCODE_URL,
                params={"q": location, "format": "json", "limit": 1},
                headers={"User-Agent": "PuffPAI/1.0"},
                timeout=10,
            )
            data = resp.json()
            coords = None
            if data:
                coords = valid_pair(data[0].get("lat"), data[0].get("lon"))
        except _doors.EgressBlocked as blocked:
            logger.debug("household-geo: geocode door shut: %s", blocked.reason)
            return None
        except Exception as exc:
            # Type only: a requests error message quotes the URL, and the URL
            # carries the place name.
            logger.debug("household-geo: geocode failed: %s", type(exc).__name__)
            coords = None

        _cache.clear()
        _cache.update({"location": location, "coords": coords,
                       "at": time.monotonic()})
    if coords is None:
        logger.info("household-geo: no coordinates found for the configured "
                    "location; will not retry for %ds", NEGATIVE_TTL)
    return coords


def resolve() -> dict:
    """``{"lat", "lon", "source", "location"}``: ``lat``/``lon`` are ``None``
    and ``source`` is ``None`` when the house's position is unknown."""
    location = household_location()
    coords = configured_coordinates()
    if coords is not None:
        return {"lat": coords[0], "lon": coords[1],
                "source": SOURCE_CONFIG, "location": location}
    if location:
        coords = _geocode(location)
        if coords is not None:
            return {"lat": coords[0], "lon": coords[1],
                    "source": SOURCE_GEOCODE, "location": location}
    return {"lat": None, "lon": None, "source": None, "location": location}


def household_coordinates():
    """``(lat, lon)`` for this house, or ``(None, None)`` when unknown.

    Order: the config fields, then one cached geocode of the configured place
    name through the ``geocode`` door if it is open. Never a built-in default:
    ``(None, None)`` is the only honest answer when neither is available,
    and the caller must say so rather than show another city's weather.
    """
    r = resolve()
    return r["lat"], r["lon"]


def reset_cache() -> None:
    """Forget any geocode result (tests; config reloads)."""
    with _lock:
        _cache.clear()
