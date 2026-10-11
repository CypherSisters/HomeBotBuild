"""
PuffPAI Home Blueprint — the layout the owner drew
==================================================

Home Assistant knows which *area* a device is in. It does not know where that
area is on the floor, how big it is, or which floor it sits on, and it never
will: the everyday HA user this appliance holds is deliberately non-admin, so
PuffPAI cannot write areas back even if it wanted to. The blueprint page needs
geometry, and for the many devices HA has no area for at all it needs a place
to record the owner's own answer.

That answer lives here — PuffPAI-side, one file, never written to HA:

``$PUFFPAI_DATA_DIR/state/config/home_layout.json`` (the same
``config_dir()`` convention ``profiles.json`` follows; see
:mod:`puffpai.statedir` for why the path is resolved per call and never at
import).

Two halves, both small on purpose:

* :func:`validate` — a strict, pure ``(ok, reason)`` check over a decoded body.
  Strict because the writer is an HTTP PUT: an unbounded ``placements`` dict or
  a 5 MB room name is a disk-filling primitive, and an unknown key is either a
  dashboard that has drifted from the contract or a caller probing for one.
* :func:`save` — tmp + ``os.replace``, so a crash mid-write never leaves the
  page with a truncated floorplan.

Nothing in this module reaches Home Assistant, the network, or any other
store. :mod:`puffpai.home_topology` reads the placements from here to answer
``GET /ha/home``; that is the only consumer in the tree.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any

from puffpai.statedir import resolve_runtime_dir

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_CONFIG_DIR = os.path.join(_REPO_ROOT, "config")

#: Test/ops override hook, exactly as :mod:`puffpai.dashboard_registry` does
#: it: set it to a path and the accessor returns that verbatim.
_LAYOUT_PATH: str | None = None

#: Hard ceiling on a PUT body, before parsing. 256 KB is ~50x the largest
#: honest layout (64 rooms + 1024 placements) and still small enough that the
#: whole file is one read.
MAX_BYTES = 256 * 1024

MAX_FLOORS = 8
MAX_ROOMS = 64
MAX_PLACEMENTS = 1024
MAX_NAME = 64
#: Grid units, ~1 ft each. 200 is a 200-foot house on a side.
MAX_GRID = 200

#: Magnitude ceiling applied to *every* integer this validator inspects, before
#: anything converts one to a float. ``float(10**400)`` raises OverflowError,
#: and the caller is an HTTP PUT: a body that makes the validator raise is a
#: 500 on a route whose whole job is to answer 400. Python compares int to
#: float exactly, so the range checks below are safe once the magnitude is.
MAX_MAGNITUDE = 10 ** 9

#: Ids: HA area/device ids, our ``puff_`` rooms, and the ``entity:<entity_id>``
#: form :mod:`puffpai.home_topology` gives device-less entities — hence ``:``.
ID_RE = re.compile(r"^[a-z0-9_:.\-]{1,128}$")

_TOP_KEYS = {"version", "floors", "rooms", "placements", "updated_at", "seeded"}
_FLOOR_KEYS = {"floor_id", "name", "level"}
_ROOM_KEYS = {"area_id", "name", "floor_id", "x", "y", "w", "h", "custom"}
_PLACEMENT_KEYS = {"area_id", "x", "y"}

_lock = threading.Lock()


def layout_path() -> str:
    """Absolute path of home_layout.json (runtime state; follows the env var)."""
    return _LAYOUT_PATH or os.path.join(
        resolve_runtime_dir(_DEFAULT_CONFIG_DIR, env_subdir="config"),
        "home_layout.json")


def empty() -> dict[str, Any]:
    """The no-file answer. The dashboard seeds a schematic from ``/ha/home``."""
    return {"version": 1, "floors": [], "rooms": [], "placements": {},
            "seeded": False}


# ---------------------------------------------------------------------------
# Validation — pure, and the only thing standing between a PUT and the disk
# ---------------------------------------------------------------------------

def _is_int(value: Any) -> bool:
    # ``bool`` is an ``int`` in Python and "x": true is not a coordinate.
    # The magnitude clamp is part of the type test on purpose: JSON has no
    # integer width, so ``{"x": 10**400}`` decodes to a perfectly good Python
    # int that no later arithmetic here may touch.
    return (isinstance(value, int) and not isinstance(value, bool)
            and -MAX_MAGNITUDE <= value <= MAX_MAGNITUDE)


def _has_control(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _bad_number(value: Any, what: str, low: float, high: float) -> str:
    """"" when *value* is a real number within ``[low, high]``, else the reason.

    Total by construction, and in this order for a reason:

    1. ``bool`` first — ``True`` is an ``int`` and ``{"x": true}`` is not a
       coordinate;
    2. then the int/float type test, so a string or a list is a 400;
    3. then, for ints, the magnitude clamp **before** any float conversion —
       ``float(10**400)`` raises OverflowError, which would escape
       :func:`validate` and turn the PUT into a 500;
    4. then NaN/inf for floats, which no comparison would have caught: NaN
       fails every ordering test silently and ``inf`` is a range answer nobody
       asked for.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"{what} must be a number"
    if isinstance(value, int):
        if not -MAX_MAGNITUDE <= value <= MAX_MAGNITUDE:
            return f"{what} out of range"
    elif not math.isfinite(value):
        return f"{what} out of range"
    if not low <= value <= high:
        return f"{what} out of range"
    return ""


def _bad_name(value: Any) -> str:
    """"" when *value* is an acceptable display name, else the reason.

    Control characters are refused rather than stripped: the name comes back
    out of this file into an SVG label, and silently rewriting what the owner
    typed hides the fact that something sent bytes a name cannot contain.
    """
    if not isinstance(value, str):
        return "name must be a string"
    if len(value) > MAX_NAME:
        return f"name longer than {MAX_NAME} characters"
    if _has_control(value):
        return "name contains control characters"
    return ""


def _bad_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        return f"invalid {what}"
    return ""


def validate(payload: Any) -> tuple[bool, str]:
    """``(ok, reason)`` for a decoded layout body. Never raises, never writes.

    "Never raises" is enforced here rather than merely documented: the body
    below is written to be total, and this wrapper is the belt to that pair of
    braces. Every check is a *reject* path, so the only thing an unexpected
    exception can mean is that a body reached an expression nobody predicted —
    and the safe answer to that is 400, not a 500 that tells the caller they
    found one. The traceback goes to the log; the caller gets four words.
    """
    try:
        return _validate(payload)
    except Exception:                              # pragma: no cover - defensive
        logger.warning("home layout validation raised; refusing the body",
                       exc_info=True)
        return False, "invalid layout"


def _validate(payload: Any) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "layout must be an object"
    unknown = set(payload) - _TOP_KEYS
    if unknown:
        return False, f"unknown key: {sorted(unknown)[0]}"
    if payload.get("version") != 1 or not _is_int(payload.get("version")):
        return False, "version must be 1"

    floors = payload.get("floors", [])
    if not isinstance(floors, list):
        return False, "floors must be a list"
    if len(floors) > MAX_FLOORS:
        return False, f"more than {MAX_FLOORS} floors"
    floor_ids: set[str] = set()
    for floor in floors:
        if not isinstance(floor, dict):
            return False, "floor must be an object"
        unknown = set(floor) - _FLOOR_KEYS
        if unknown:
            return False, f"unknown floor key: {sorted(unknown)[0]}"
        bad = _bad_id(floor.get("floor_id"), "floor_id")
        if bad:
            return False, bad
        if floor["floor_id"] in floor_ids:
            return False, f"duplicate floor_id: {floor['floor_id']}"
        floor_ids.add(floor["floor_id"])
        bad = _bad_name(floor.get("name", ""))
        if bad:
            return False, f"floor {bad}"
        level = floor.get("level", 0)
        if not _is_int(level) or not -MAX_FLOORS <= level <= MAX_FLOORS:
            return False, "floor level out of range"

    rooms = payload.get("rooms", [])
    if not isinstance(rooms, list):
        return False, "rooms must be a list"
    if len(rooms) > MAX_ROOMS:
        return False, f"more than {MAX_ROOMS} rooms"
    seen_rooms: set[str] = set()
    for room in rooms:
        if not isinstance(room, dict):
            return False, "room must be an object"
        unknown = set(room) - _ROOM_KEYS
        if unknown:
            return False, f"unknown room key: {sorted(unknown)[0]}"
        bad = _bad_id(room.get("area_id"), "area_id")
        if bad:
            return False, bad
        if room["area_id"] in seen_rooms:
            return False, f"duplicate area_id: {room['area_id']}"
        seen_rooms.add(room["area_id"])
        bad = _bad_name(room.get("name", ""))
        if bad:
            return False, f"room {bad}"
        floor_id = room.get("floor_id")
        if floor_id is not None:
            bad = _bad_id(floor_id, "floor_id")
            if bad:
                return False, bad
        for key in ("x", "y"):
            value = room.get(key, 0)
            if not _is_int(value) or not 0 <= value <= MAX_GRID:
                return False, f"room {key} out of range"
        for key in ("w", "h"):
            value = room.get(key, 1)
            if not _is_int(value) or not 1 <= value <= MAX_GRID:
                return False, f"room {key} out of range"
        if "custom" in room and not isinstance(room["custom"], bool):
            return False, "room custom must be a boolean"

    placements = payload.get("placements", {})
    if not isinstance(placements, dict):
        return False, "placements must be an object"
    if len(placements) > MAX_PLACEMENTS:
        return False, f"more than {MAX_PLACEMENTS} placements"
    for device_id, placement in placements.items():
        bad = _bad_id(device_id, "device_id")
        if bad:
            return False, bad
        if not isinstance(placement, dict):
            return False, "placement must be an object"
        unknown = set(placement) - _PLACEMENT_KEYS
        if unknown:
            return False, f"unknown placement key: {sorted(unknown)[0]}"
        bad = _bad_id(placement.get("area_id"), "area_id")
        if bad:
            return False, bad
        for key in ("x", "y"):
            if key not in placement:
                continue
            bad = _bad_number(placement[key], f"placement {key}", 0.0, 1.0)
            if bad:
                return False, bad

    # ``seeded`` and ``updated_at`` are both discarded by :func:`save` — they
    # are accepted only so the dashboard can PUT back the body it GET'd. That
    # is not a reason to skip checking them: an unchecked key is still a key
    # this validator vouched for, and the next hand to read this file may not
    # know it was never stored.
    if "seeded" in payload and not isinstance(payload["seeded"], bool):
        return False, "seeded must be a boolean"

    updated_at = payload.get("updated_at", "")
    if (not isinstance(updated_at, str) or len(updated_at) > MAX_NAME
            or _has_control(updated_at)):
        return False, "invalid updated_at"

    try:
        size = len(json.dumps(payload).encode("utf-8"))
    except (TypeError, ValueError):
        return False, "layout is not serialisable"
    if size > MAX_BYTES:
        return False, f"layout larger than {MAX_BYTES} bytes"
    return True, ""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def load() -> dict[str, Any]:
    """The stored layout, or :func:`empty` when there is none. Never raises."""
    path = layout_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return empty()
    if not isinstance(data, dict):
        return empty()
    result = empty()
    result.update({k: v for k, v in data.items() if k in _TOP_KEYS})
    result["version"] = 1
    result["seeded"] = True
    return result


def save(payload: dict[str, Any]) -> dict[str, Any]:
    """Write a *validated* layout atomically and return what was stored.

    ``seeded`` is not stored — it describes the file's existence, not its
    contents, and a stored ``false`` would lie the moment it was written.
    """
    stored = {
        "version": 1,
        "floors": list(payload.get("floors", [])),
        "rooms": list(payload.get("rooms", [])),
        "placements": dict(payload.get("placements", {})),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = layout_path()
    with _lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(stored, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    return {**stored, "seeded": True}


def placements() -> dict[str, dict[str, Any]]:
    """``device_id -> placement`` from the stored layout. Never raises."""
    found = load().get("placements", {})
    return found if isinstance(found, dict) else {}


__all__ = [
    "MAX_BYTES", "MAX_FLOORS", "MAX_ROOMS", "MAX_PLACEMENTS", "MAX_NAME",
    "MAX_GRID", "ID_RE",
    "layout_path", "empty", "validate", "load", "save", "placements",
]
