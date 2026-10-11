"""The household's home point, set once by the owner, kept on this box only.

The exact latitude/longitude of the
house is the most sensitive single fact the appliance holds about where its
family sleeps, so it never ships in a tracked file and never reaches a log:

* It lives in ONE local file, ``home_point.json``, in the box's writable
  config-state directory (the ``config_dir()`` convention ``profiles.json``
  follows: ``$PUFFPAI_DATA_DIR/config/``, else ``config/`` under the
  per-user data dir). On the appliance units it is never under
  ``/home``: puffpai-api runs with ``ProtectHome``.
* Written atomically (temp file in the same directory + ``os.replace``) with
  mode 0600, so a crash mid-write never leaves half a point and no other
  account on the box can read it.
* :mod:`puffpai.config` layers it over ``puffpai_config.json`` at load, so
  ``cfg.latitude``/``cfg.longitude`` come from here when it is present, and
  :mod:`puffpai.household_geo` (weather card, alerts, forecast tool) reads them
  from there. :func:`puffpai.config.refresh_home_point` re-layers the running
  singleton after an owner edit, so no restart is needed.

Nothing in this module logs a coordinate, echoes caller input in an error, or
returns the numbers anywhere except to the config loader.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: Basename of the owner overlay.
OVERLAY_NAME = "home_point.json"

#: Marks the file as what it is, so a reader pointed at the wrong file says so.
OVERLAY_KIND = "home_point"

#: Owner-only. The point says where the family sleeps.
FILE_MODE = 0o600
DIR_MODE = 0o750

SOURCE_OWNER = "owner"
SOURCE_CONFIG = "config"

#: Longest ``point`` string accepted. A phone's "lat, lon" copy is ~25 chars;
#: anything far longer is not a point and is refused before the regex runs.
MAX_POINT_LEN = 64

#: "lat, lon" as copied from a phone maps app: optional spaces, optional sign,
#: optional trailing degree sign after either number.
_POINT_RE = re.compile(
    r"^\s*([+-]?\d{1,3}(?:\.\d{1,12})?)\s*°?\s*,"
    r"\s*([+-]?\d{1,3}(?:\.\d{1,12})?)\s*°?\s*$"
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

#: Test/ops hook, same shape as ``egress_doors.set_overlay_override``.
_OVERRIDE: str | None = None

_lock = threading.Lock()


class HomePointError(ValueError):
    """A refused point. ``str(exc)`` is a fixed sentence: it never contains
    the caller's input, so it is safe to return to the browser or to log."""


# Fixed messages. Deliberately generic: none of them can carry the input.
ERR_SHAPE = ("Send either latitude and longitude as numbers, or one point "
             "like \"lat, lon\".")
ERR_NUMBER = "Latitude and longitude must be plain numbers."
ERR_POINT = ("That doesn't look like a point. Paste two numbers separated "
             "by a comma, latitude first.")
ERR_RANGE = ("That point is out of range. Latitude goes from -90 to 90 and "
             "longitude from -180 to 180.")
ERR_ZERO = "0, 0 is not a home point. Paste the numbers for your home."


def set_path_override(path: str | None) -> None:
    """Point the overlay at *path* (tests, and an operator moving the file)."""
    global _OVERRIDE
    _OVERRIDE = path


def overlay_path() -> str:
    """Absolute path of the overlay. Resolved per call, never at import."""
    if _OVERRIDE:
        return _OVERRIDE
    from puffpai.statedir import resolve_runtime_dir
    return os.path.join(
        resolve_runtime_dir(os.path.join(_REPO_ROOT, "state", "config"),
                            env_subdir="config"),
        OVERLAY_NAME)


# ── validation (the trust boundary) ─────────────────────────────────────────

def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HomePointError(ERR_NUMBER)
    try:
        f = float(value)
    except OverflowError:  # an int too large for a float is not a coordinate
        raise HomePointError(ERR_NUMBER) from None
    if not math.isfinite(f):
        raise HomePointError(ERR_NUMBER)
    return f


def _check_range(lat: float, lon: float) -> tuple:
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise HomePointError(ERR_RANGE)
    if lat == 0.0 and lon == 0.0:
        raise HomePointError(ERR_ZERO)
    return lat, lon


def parse_point_string(text) -> tuple:
    """``"lat, lon"`` (a phone maps copy) to floats, or :class:`HomePointError`."""
    if not isinstance(text, str) or len(text) > MAX_POINT_LEN:
        raise HomePointError(ERR_POINT)
    m = _POINT_RE.match(text)
    if not m:
        raise HomePointError(ERR_POINT)
    return _check_range(float(m.group(1)), float(m.group(2)))


def parse_body(body) -> tuple:
    """Validate a POST body: exactly ``{latitude, longitude}`` (numbers) or
    exactly ``{point: "lat, lon"}``. Anything else is refused."""
    if not isinstance(body, dict):
        raise HomePointError(ERR_SHAPE)
    keys = set(body)
    if keys == {"point"}:
        return parse_point_string(body["point"])
    if keys == {"latitude", "longitude"}:
        return _check_range(_number(body["latitude"]),
                            _number(body["longitude"]))
    raise HomePointError(ERR_SHAPE)


def valid_pair(lat, lon):
    """Both usable numbers (not 0/0) as floats; else ``None``. Never raises."""
    try:
        return _check_range(_number(lat), _number(lon))
    except HomePointError:
        return None


# ── the file ────────────────────────────────────────────────────────────────

def read(path: str | None = None):
    """``{"latitude", "longitude", "set_at"}`` from the overlay, or ``None``.

    ``None`` covers both "no file" (normal on a fresh box) and "file there but
    unusable". An unusable file is logged by path only, never its content, and
    is treated as no point at all: a file we cannot read is not a location.
    """
    path = path or overlay_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("home point file %s is unreadable (%s); treating the "
                       "home point as not set", path, type(exc).__name__)
        return None
    if not isinstance(raw, dict) or raw.get("kind") != OVERLAY_KIND:
        logger.warning("home point file %s is not a home point file; treating "
                       "the home point as not set", path)
        return None
    pair = valid_pair(raw.get("latitude"), raw.get("longitude"))
    if pair is None:
        logger.warning("home point file %s holds no usable point; treating "
                       "the home point as not set", path)
        return None
    set_at = raw.get("set_at") if isinstance(raw.get("set_at"), str) else None
    return {"latitude": pair[0], "longitude": pair[1], "set_at": set_at}


def write(lat: float, lon: float, path: str | None = None) -> str:
    """Store the point atomically with mode 0600. Returns the ``set_at`` stamp.

    Re-validates: this function is the last thing before the disk, and a
    caller that skipped :func:`parse_body` must not be able to store junk.
    """
    lat, lon = _check_range(_number(lat), _number(lon))
    path = path or overlay_path()
    set_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    payload = {"kind": OVERLAY_KIND, "version": 1, "latitude": lat,
               "longitude": lon, "set_at": set_at}
    parent = os.path.dirname(path)
    with _lock:
        os.makedirs(parent, mode=DIR_MODE, exist_ok=True)
        # mkstemp creates the file 0600 from the start, so there is no window
        # in which the point sits in a world-readable temp file.
        fd, tmp = tempfile.mkstemp(prefix=".home_point.", suffix=".tmp",
                                   dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, FILE_MODE)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    logger.info("home point saved by the owner")
    return set_at


def clear(path: str | None = None) -> bool:
    """Remove the overlay. True if a file was removed."""
    path = path or overlay_path()
    with _lock:
        try:
            os.remove(path)
        except FileNotFoundError:
            return False
    logger.info("home point cleared by the owner")
    return True


def status(cfg=None) -> dict:
    """``{set, set_at, source}``. NEVER the numbers.

    ``source`` is ``"owner"`` when the overlay holds a point, ``"config"`` when
    only ``puffpai_config.json`` carries one (an operator-set point), else
    ``None``.
    """
    owner = read()
    if owner is not None:
        return {"set": True, "set_at": owner["set_at"], "source": SOURCE_OWNER}
    if cfg is None:
        try:
            from puffpai.config import get_config
            cfg = get_config()
        except Exception:  # pragma: no cover - config always loads on box
            cfg = None
    raw = getattr(cfg, "_raw", None) or {}
    from puffpai.config import config_file_coordinates
    if config_file_coordinates(raw) is not None:
        return {"set": True, "set_at": None, "source": SOURCE_CONFIG}
    return {"set": False, "set_at": None, "source": None}
