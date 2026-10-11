"""ONE household roster — the people this appliance is for.

Why this module exists
----------------------
The household appears in five places, and left alone they disagree:

* ``profiles.json`` (profile_manager): the real list, whose OWNER row ships
  the placeholder ``display_name: "Owner"``;
* ``/data/profile.json`` (legacy single-user ``GET/PUT /profile``) — where the
  owner's real name may actually be typed;
* the Calendar's ``family`` list: four placeholders (Dad, Mom, Alex, Emma)
  that match nobody's real household;
* FitFam's ``users``: keyed by whatever names were first logged, so "log my
  workout" may not find the owner;
* Trivia's ``players``.

So the appliance could say "Owner", the calendar "Mom", and fitness
"unknown user" — about the same person, in the same hour.

This module is the single source of truth: profiles.json IS the household,
every other store is a projection kept in step by
:func:`sync_household_consumers`, and the owner's name is healed from the
legacy profile the first time anything reads the roster.

Discipline
----------
* Never deletes or rewrites a consumer store wholesale. Members are ADDED to
  FitFam/Trivia; the Calendar's family list is the one thing replaced, and
  even there any row an event or task still references is kept so no stored
  data is orphaned.
* Resolves every path per call (see :mod:`puffpai.statedir`), never at import.
* Raises :class:`HouseholdError` with a stable ``code`` for every caller
  error, so the routes answer 400/404/409 without re-deriving the reason.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from puffpai.statedir import resolve_runtime_dir

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The four roles a member can have. "owner" is not assignable through the
#: API: exactly one row owns the box, and it is the one that already does.
ROLES = ("owner", "adult", "child", "pet")

#: What ``display_name`` says when nobody has ever typed a name into
#: profiles.json. Left as is, "log my workout" and every spoken greeting
#: call the owner "Owner".
OWNER_PLACEHOLDER = "Owner"

NAME_MIN = 1
NAME_MAX = 40

_DEFAULT_EMOJI = {
    "owner": "\U0001f9d1",   # 🧑
    "adult": "\U0001f9d1",   # 🧑
    "child": "\U0001f9d2",   # 🧒
    "pet": "\U0001f43e",     # 🐾
}

#: Deterministic colour assignment, so a member keeps the same colour across
#: rebuilds (index into the palette by position in the roster).
_PALETTE = ("#38bdf8", "#a78bfa", "#4ade80", "#fbbf24", "#f472b6", "#fb923c",
            "#22d3ee", "#c084fc")

#: Avatars are stored as ONE normalised jpeg per member.
AVATAR_MAX_BYTES = 8 * 1024 * 1024
AVATAR_PX = 512
THUMB_PX = 256

#: Magic numbers for the formats the avatar endpoints accept. Checked even
#: when Pillow is present: the decode happens after this, never before.
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)

_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heic",
    ".gif": "image/gif", ".bmp": "image/bmp", ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


class HouseholdError(ValueError):
    """A caller error with a stable code: ``invalid_name``, ``invalid_role``,
    ``duplicate``, ``not_found``, ``owner_undeletable``, ``not_an_image``,
    ``too_large``, ``upload_not_found``."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


# ── paths ────────────────────────────────────────────────────────────────────

def avatars_dir() -> Path:
    """``<data_dir>/avatars`` — created by the write sites, not here."""
    return Path(resolve_runtime_dir(
        str(_REPO_ROOT / "data" / "avatars"), env_subdir="avatars"))


def avatar_path(member_id: str) -> Path:
    return avatars_dir() / f"{_safe_component(member_id)}.jpg"


def thumbs_dir() -> Path:
    """Generated upload thumbnails — a cache, beside the avatars."""
    return avatars_dir() / "thumbs"


def thumb_path(upload_id: str) -> Path:
    return thumbs_dir() / f"{_safe_component(upload_id)}.jpg"


def legacy_profile_path() -> str:
    """``data/profile.json`` — the single-user profile ``GET/PUT /profile``
    reads. Taken from ``api_server`` when it is loaded so a test that
    redirects that constant redirects this too."""
    api = sys.modules.get("puffpai.api_server")
    path = getattr(api, "_PROFILE_PATH", None) if api is not None else None
    if isinstance(path, (str, os.PathLike)):
        return os.fspath(path)
    return str(_REPO_ROOT / "data" / "profile.json")


def _safe_component(value: str) -> str:
    """A filename component that cannot escape its directory."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or ""))
    cleaned = cleaned.lstrip(".") or "unknown"
    return cleaned[:80]


# ── the roster ───────────────────────────────────────────────────────────────

def slug(name: str) -> str:
    """``"Riley Reyes"`` -> ``"riley_reyes"``. Stable, lowercase, ASCII-safe."""
    base = re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")
    return base or "member"


def _manager():
    from puffpai.profile_manager import ProfileManager
    return ProfileManager()


def _legacy_owner_name() -> str:
    try:
        with open(legacy_profile_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    name = str((data or {}).get("name") or "").strip()
    return "" if name.lower() == OWNER_PLACEHOLDER.lower() else name


def _owner_row(profiles: list) -> dict | None:
    """The owner: the row that says so, else the oldest row."""
    for row in profiles:
        if str((row or {}).get("role") or "").lower() == "owner":
            return row
    ordered = [r for r in profiles if isinstance(r, dict)]
    if not ordered:
        return None
    return min(ordered, key=lambda r: str(r.get("created") or ""))


def _normalise(mgr) -> list:
    """Backfill ``role``/``emoji``/``color`` and heal the owner's name.

    Persists when anything changed — the heal has to stick, or every read
    would keep re-deriving a name the store still contradicts.
    """
    profiles = [p for p in mgr._profiles if isinstance(p, dict)]
    owner = _owner_row(profiles)
    changed = False
    for index, row in enumerate(profiles):
        role = str(row.get("role") or "").lower()
        if row is owner and role != "owner":
            row["role"] = "owner"
            changed = True
        elif row is not owner and role not in ROLES:
            row["role"] = "adult"
            changed = True
        elif row is not owner and role == "owner":
            # Two owners is not a state this module can answer from.
            row["role"] = "adult"
            changed = True
        if not row.get("emoji"):
            row["emoji"] = _DEFAULT_EMOJI.get(row.get("role") or "adult", "\U0001f9d1")
            changed = True
        if not row.get("color"):
            row["color"] = _PALETTE[index % len(_PALETTE)]
            changed = True
    if owner is not None:
        current = str(owner.get("display_name") or "").strip()
        if not current or current.lower() == OWNER_PLACEHOLDER.lower():
            healed = _legacy_owner_name()
            if healed:
                owner["display_name"] = healed
                if not owner.get("avatar_seed") or \
                        str(owner.get("avatar_seed")).lower() == OWNER_PLACEHOLDER.lower():
                    owner["avatar_seed"] = healed
                changed = True
                logger.info("household: healed the owner's name from the "
                            "legacy profile")
    if changed:
        try:
            mgr._save_profiles()
        except OSError as exc:      # pragma: no cover - read-only state dir
            logger.warning("household: could not persist the roster: %s", exc)
    return profiles


def _member(row: dict) -> dict:
    member_id = str(row.get("id") or "")
    return {
        "id": member_id,
        "display_name": str(row.get("display_name") or "").strip(),
        "role": str(row.get("role") or "adult"),
        "emoji": row.get("emoji") or "\U0001f9d1",
        "color": row.get("color") or _PALETTE[0],
        "avatar_url": (f"/household/{member_id}/avatar"
                       if avatar_path(member_id).exists() else None),
        "preferences": dict(row.get("preferences") or {}),
        "created": row.get("created"),
        "last_active": row.get("last_active"),
    }


def members() -> list:
    """Every member, owner first, then by creation order. Never raises."""
    try:
        mgr = _manager()
        rows = _normalise(mgr)
    except Exception as exc:        # pragma: no cover - defensive
        logger.debug("household roster unavailable: %s", exc)
        return []
    owner = _owner_row(rows)
    ordered = ([owner] if owner is not None else []) + \
        sorted((r for r in rows if r is not owner),
               key=lambda r: str(r.get("created") or ""))
    return [_member(r) for r in ordered]


def owner_id() -> str | None:
    """The id of the row that owns this box, or None when there are none."""
    try:
        rows = _normalise(_manager())
    except Exception as exc:        # pragma: no cover - defensive
        logger.debug("household owner unavailable: %s", exc)
        return None
    owner = _owner_row(rows)
    return str(owner.get("id")) if owner else None


def active_id() -> str | None:
    """The active profile id: ``last_active_profile`` when it still names a
    real row, else the owner. Never raises."""
    try:
        mgr = _manager()
        rows = _normalise(mgr)
    except Exception as exc:        # pragma: no cover - defensive
        logger.debug("household active id unavailable: %s", exc)
        return None
    last = mgr.get_last_active_id()
    if last and any(str(row.get("id")) == str(last) for row in rows):
        return str(last)
    owner = _owner_row(rows)
    return str(owner.get("id")) if owner else None


def set_active(member_id) -> dict:
    """Mark *member_id* as the active profile. 404 if unknown."""
    mgr = _manager()
    _normalise(mgr)
    row = mgr.get_profile(str(member_id))
    if row is None:
        raise HouseholdError("not_found", "No such household member.")
    mgr.set_last_active(str(member_id))
    return get_member(str(member_id))


def get_member(member_id: str) -> dict | None:
    for member in members():
        if member["id"] == str(member_id):
            return member
    return None


def names() -> list:
    """Just the display names, roster order — for the spoken read."""
    return [m["display_name"] for m in members() if m["display_name"]]


def _validate_name(display_name: str, *, exclude_id: str | None = None) -> str:
    name = str(display_name or "").strip()
    if len(name) < NAME_MIN or len(name) > NAME_MAX:
        raise HouseholdError(
            "invalid_name",
            f"A name has to be {NAME_MIN}-{NAME_MAX} characters.")
    for row in members():
        if exclude_id and row["id"] == exclude_id:
            continue
        if row["display_name"].lower() == name.lower():
            raise HouseholdError("duplicate",
                                 f"{row['display_name']} is already in the "
                                 "household.")
    return name


def _validate_role(role: str | None, *, default: str = "adult") -> str:
    value = str(role or default).strip().lower()
    if value == "owner":
        # Assignable only by being the row that already owns the box.
        raise HouseholdError("invalid_role",
                             "There is already an owner of this box.")
    if value not in ROLES:
        raise HouseholdError(
            "invalid_role",
            "Role has to be one of: " + ", ".join(r for r in ROLES if r != "owner"))
    return value


def _unique_id(existing: set, base: str) -> str:
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def create_member(display_name, role=None, emoji=None, color=None) -> dict:
    """Add a person (or a pet) to the household and propagate them."""
    name = _validate_name(display_name)
    resolved_role = _validate_role(role)
    mgr = _manager()
    _normalise(mgr)
    existing = {str(p.get("id")) for p in mgr._profiles}
    member_id = _unique_id(existing, slug(name))
    index = len(mgr._profiles)
    mgr.create_profile(
        name, profile_id=member_id,
        extra={
            "role": resolved_role,
            "emoji": emoji or _DEFAULT_EMOJI[resolved_role],
            "color": color or _PALETTE[index % len(_PALETTE)],
        })
    logger.info("household: added %s (%s)", name, resolved_role)
    sync_household_consumers()
    return get_member(member_id)


#: "not sent" — distinct from ``None``, which on emoji/colour means "clear
#: it". A partial update is the normal shape (the dashboard sends one field
#: at a time), so omission and an explicit null must not collapse into one.
UNSET = object()

#: theme values the dashboard may set.
THEME_VALUES = ("day", "plum", "light", "night", "auto")


def _merge_preferences(existing: dict | None, patch: dict) -> dict:
    """Merge *patch* into *existing* preferences (top-level keys).

    ``None`` removes a key, for any key. ``theme`` is the one key this
    version acts on, and is validated against :data:`THEME_VALUES`. Any
    other key is accepted (and ignored) when its value is a scalar — a
    dashboard ahead of this build should not 400 on a field we don't read
    yet — but a non-scalar value is the caller getting the shape wrong,
    hence a 400.
    """
    merged = dict(existing or {})
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
            continue
        if key == "theme":
            if not isinstance(value, str) or value not in THEME_VALUES:
                raise HouseholdError(
                    "invalid_preference",
                    "theme has to be one of: " + ", ".join(THEME_VALUES))
            merged[key] = value
            continue
        if not isinstance(value, (str, int, float, bool)):
            raise HouseholdError(
                "invalid_preference",
                f"{key} has to be a plain value.")
        # a recognised-shape, unhandled key: accepted, not yet acted on.
    return merged


def update_member(member_id, display_name=UNSET, role=UNSET, emoji=UNSET,
                  color=UNSET, preferences=UNSET) -> dict:
    """Change a member's name/role/emoji/colour. The owner keeps its role.

    Every field is optional. An explicit ``None`` for ``emoji``/``color``
    clears it, and the roster serves the role default again.
    """
    mgr = _manager()
    _normalise(mgr)
    row = mgr.get_profile(str(member_id))
    if row is None:
        raise HouseholdError("not_found", "No such household member.")
    fields = {}
    if display_name is not UNSET:
        fields["display_name"] = _validate_name(display_name,
                                                exclude_id=str(member_id))
    if role is not UNSET:
        if str(row.get("role") or "") == "owner":
            raise HouseholdError("invalid_role",
                                 "The owner's role cannot be changed.")
        fields["role"] = _validate_role(role)
    if emoji is not UNSET:
        fields["emoji"] = "" if emoji is None else str(emoji)[:8]
    if color is not UNSET:
        fields["color"] = "" if color is None else str(color)[:32]
    if fields:
        mgr.update_profile(str(member_id), **fields)
    if preferences is not UNSET:
        if not isinstance(preferences, dict):
            raise HouseholdError("invalid_preference",
                                 "preferences has to be an object.")
        row["preferences"] = _merge_preferences(row.get("preferences"),
                                                preferences)
        mgr._save_profiles()
    sync_household_consumers()
    return get_member(str(member_id))


def delete_member(member_id) -> dict:
    """Remove a member. The owner is never removable."""
    mgr = _manager()
    _normalise(mgr)
    row = mgr.get_profile(str(member_id))
    if row is None:
        raise HouseholdError("not_found", "No such household member.")
    if str(row.get("role") or "") == "owner":
        raise HouseholdError("owner_undeletable",
                             "The owner of this box can't be removed.")
    removed = _member(row)
    mgr.delete_profile(str(member_id))
    delete_avatar(str(member_id))
    logger.info("household: removed %s", removed["display_name"])
    sync_household_consumers()
    return removed


def set_owner_name(name: str) -> None:
    """Write the owner's name into profiles.json (``PUT /profile`` calls this).

    Best-effort: the legacy profile write is the one the caller asked for, and
    it must not fail because the roster could not be updated.
    """
    cleaned = str(name or "").strip()
    if not cleaned:
        return
    try:
        mgr = _manager()
        rows = _normalise(mgr)
        owner = _owner_row(rows)
        if owner is None or str(owner.get("display_name") or "") == cleaned:
            return
        owner["display_name"] = cleaned
        mgr._save_profiles()
        logger.info("household: owner profile renamed from /profile")
        sync_household_consumers()
    except Exception as exc:
        logger.warning("household: owner rename did not reach the roster: %s",
                       exc)


def fitfam_keys_for(display_name: str) -> list:
    """Candidate FitFam keys for *display_name*, best first.

    ``fitfam_key`` (explicit) and the legacy ``family_id``, then the profile
    id, then the slug of the name. Matching on display_name alone,
    which is exactly what the placeholder "Owner" broke.
    """
    wanted = str(display_name or "").strip().lower()
    if not wanted:
        return []
    out = []
    for row in members():
        if row["display_name"].lower() != wanted:
            continue
        profile = None
        try:
            profile = _manager().get_profile(row["id"])
        except Exception as exc:    # pragma: no cover - defensive
            logger.debug("profile lookup failed: %s", exc)
        for key in ("fitfam_key", "family_id"):
            value = str((profile or {}).get(key) or "").strip()
            if value and value not in out:
                out.append(value)
        if row["id"] not in out:
            out.append(row["id"])
        break
    candidate = slug(wanted)
    if candidate not in out:
        out.append(candidate)
    return out


# ── the consumer stores ──────────────────────────────────────────────────────

def _app_data(app=None):
    if app is not None:
        return app
    api = sys.modules.get("puffpai.api_server")
    if api is not None and hasattr(api, "_get_app_data"):
        shared = api._get_app_data()
        if shared is not None:
            return shared
    from puffpai.app_data import AppData
    return AppData()


def _empty_fitfam_user(users: dict, member: dict) -> dict:
    """A new FitFam slice, shaped like the ones already in the store.

    The shape is COPIED from an existing member (so a store that grew fields
    keeps them) with every history/log collection emptied and every counter
    zeroed. ``prefs.level`` is the one preference carried over — it is the
    programme difficulty default, not somebody else's programme.
    """
    template = None
    for row in users.values():
        if isinstance(row, dict):
            template = row
            break
    level = str(((template or {}).get("prefs") or {}).get("level") or "mid")
    fresh = {}
    for key, value in (template or {}).items():
        if key in ("name", "prefs"):
            continue
        if isinstance(value, list):
            fresh[key] = []
        elif isinstance(value, dict):
            fresh[key] = {}
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            fresh[key] = 0
        elif isinstance(value, str):
            fresh[key] = ""
        else:
            fresh[key] = None
    fresh.setdefault("goals", [])
    fresh.setdefault("history", [])
    fresh.setdefault("streak", 0)
    fresh.setdefault("totalWorkouts", 0)
    fresh["name"] = member["display_name"]
    fresh["emoji"] = member["emoji"]
    fresh["prefs"] = {"level": level}
    return fresh


def _empty_trivia_player(players: dict, display_name: str) -> dict:
    """A new Trivia player slice, shaped like the ones already in the store.

    The shape is COPIED from an existing player (so a store that grew fields
    keeps them) with every counter zeroed. ``name`` is set to the member's
    display name.
    """
    template = None
    for row in players.values():
        if isinstance(row, dict):
            template = row
            break
    fresh = {}
    for key, value in (template or {}).items():
        if key in ("name", "display_name"):
            continue
        if isinstance(value, list):
            fresh[key] = []
        elif isinstance(value, dict):
            fresh[key] = {}
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            fresh[key] = 0
        elif isinstance(value, str):
            fresh[key] = ""
        else:
            fresh[key] = None
    fresh["name"] = display_name
    return fresh


def _referenced_family_ids(calendar: dict) -> set:
    """Family ids an EVENT or TASK still points at — never orphaned."""
    referenced = set()
    for key in ("events", "tasks"):
        for row in (calendar.get(key) or []):
            if not isinstance(row, dict):
                continue
            for field in ("member", "assignee"):
                value = row.get(field)
                if isinstance(value, str) and value.strip():
                    referenced.add(value.strip())
                elif isinstance(value, list):
                    referenced.update(str(v).strip() for v in value if str(v).strip())
    return referenced


def sync_household_consumers(app=None, roster=None) -> dict:
    """Project the roster onto the Calendar, FitFam and Trivia.

    Returns ``{store: what changed}`` for the caller/tests. Never raises: a
    consumer that cannot be written is logged and the others still get their
    update.
    """
    people = roster if roster is not None else members()
    result = {}
    if not people:
        return result
    try:
        store = _app_data(app)
    except Exception as exc:        # pragma: no cover - defensive
        logger.warning("household sync: no app data (%s)", exc)
        return result

    # ── calendar.family ──────────────────────────────────────────────────
    try:
        calendar = store.get_data("calendar") or {}
        family = [{"id": m["id"], "name": m["display_name"], "role": m["role"],
                   "emoji": m["emoji"], "color": m["color"]} for m in people]
        known = {row["id"] for row in family}
        referenced = _referenced_family_ids(calendar)
        kept = [row for row in (calendar.get("family") or [])
                if isinstance(row, dict)
                and str(row.get("id") or "") not in known
                and str(row.get("id") or "") in referenced]
        family.extend(kept)
        store.set_data("calendar", {"family": family}, merge=True,
                       allow_shrink=True)
        result["calendar"] = len(family)
        logger.info("household sync: calendar family = %d member(s), %d kept "
                    "for stored events", len(people), len(kept))
    except Exception as exc:
        logger.warning("household sync: calendar not updated (%s)", exc)

    # ── fitfam.users ─────────────────────────────────────────────────────
    try:
        fitfam = store.get_data("fitfam") or {}
        users = dict(fitfam.get("users") or {})
        added = []
        for member in people:
            if member["role"] == "pet":
                continue
            key = member["id"]
            if any(k == key or str((row or {}).get("name") or "").lower()
                   == member["display_name"].lower()
                   for k, row in users.items()):
                continue
            users[key] = _empty_fitfam_user(users, member)
            added.append(key)
        if added:
            store.set_data("fitfam", {"users": users}, merge=True)
        result["fitfam"] = added
        logger.info("household sync: fitfam users +%d (%s)", len(added),
                    ", ".join(added) or "none")
    except Exception as exc:
        logger.warning("household sync: fitfam not updated (%s)", exc)

    # ── trivia.players ───────────────────────────────────────────────────
    try:
        trivia = store.get_data("trivia") or {}
        raw_players = trivia.get("players")
        added = []
        if isinstance(raw_players, dict):
            players = dict(raw_players)
            existing_names = {
                str((row or {}).get("name") or (row or {}).get("display_name") or "").lower()
                for row in players.values()
            }
            for member in people:
                if member["role"] == "pet":
                    continue
                key = member["id"]
                if key in players or member["display_name"].lower() in existing_names:
                    continue
                players[key] = _empty_trivia_player(players, member["display_name"])
                added.append(member["display_name"])
            if added:
                store.set_data("trivia", {"players": players}, merge=True)
        else:
            players = list(raw_players or [])
            existing = {str(p).lower() if not isinstance(p, dict)
                        else str(p.get("name") or "").lower() for p in players}
            for member in people:
                if member["role"] == "pet":
                    continue
                if member["display_name"].lower() in existing:
                    continue
                players.append(member["display_name"])
                added.append(member["display_name"])
            if added:
                store.set_data("trivia", {"players": players}, merge=True)
        result["trivia"] = added
        logger.info("household sync: trivia players +%d (%s)", len(added),
                    ", ".join(added) or "none")
    except Exception as exc:
        logger.warning("household sync: trivia not updated (%s)", exc)

    return result


# ── avatars ──────────────────────────────────────────────────────────────────

def _pillow():
    """The PIL Image module, or None when Pillow is not installed."""
    try:
        from PIL import Image
        return Image
    except Exception as exc:        # pragma: no cover - depends on the venv
        logger.info("household: Pillow unavailable (%s); avatars are stored "
                    "as uploaded", exc)
        return None


def sniff_image(data: bytes) -> str | None:
    """The mime type of *data* from its magic number, or None."""
    if not data:
        return None
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc",
                                               b"mif1", b"heim", b"msf1"):
        return "image/heic"
    return None


def _square_jpeg(data: bytes, size: int) -> bytes | None:
    """Centre-cropped, resized JPEG bytes — or None if Pillow cannot do it."""
    Image = _pillow()
    if Image is None:
        return None
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except Exception as exc:    # pragma: no cover - old Pillow
            logger.debug("exif transpose skipped: %s", exc)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        width, height = img.size
        edge = min(width, height)
        left = (width - edge) // 2
        top = (height - edge) // 2
        img = img.crop((left, top, left + edge, top + edge))
        img = img.resize((size, size))
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=88)
        return out.getvalue()


def save_avatar(member_id: str, data: bytes) -> dict:
    """Normalise *data* into ``<data_dir>/avatars/<id>.jpg``.

    Returns ``{"path", "bytes", "decoded"}``. ``decoded`` is False when
    Pillow is missing — the bytes are then stored as uploaded, after the
    magic-number check, rather than refusing the household an avatar over a
    missing optional dependency.
    """
    if len(data or b"") > AVATAR_MAX_BYTES:
        raise HouseholdError("too_large", "That image is over 8 MB.")
    mime = sniff_image(data)
    if mime is None:
        raise HouseholdError("not_an_image",
                             "That file isn't a JPEG, PNG, WebP or HEIC image.")
    try:
        normalised = _square_jpeg(data, AVATAR_PX)
    except Exception as exc:
        logger.info("avatar decode failed (%s); storing as uploaded", exc)
        normalised = None
    target = avatar_path(member_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = normalised if normalised is not None else data
    target.write_bytes(payload)
    return {"path": str(target), "bytes": len(payload),
            "decoded": normalised is not None, "source_mime": mime}


def delete_avatar(member_id: str) -> bool:
    path = avatar_path(member_id)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:          # pragma: no cover - defensive
        logger.warning("avatar delete failed: %s", exc)
        return False


# ── Send-to-Puff uploads, as an avatar source ────────────────────────────────

def _uploads_dir() -> Path:
    from puffpai import ingestion
    return ingestion._upload_dir()


def _library_sources_dir() -> Path:
    """Where a knowledge upload keeps the ORIGINAL of what it indexed.

    Taken from ``api_server._LIBRARY_SOURCES_DIR`` when that module is loaded
    — the same discipline as :func:`legacy_profile_path` — so this list
    follows the writer wherever the writer puts it instead of guessing a
    second path of its own.
    """
    api = sys.modules.get("puffpai.api_server")
    path = getattr(api, "_LIBRARY_SOURCES_DIR", None) if api is not None else None
    if isinstance(path, (str, os.PathLike)):
        return Path(os.fspath(path))
    return _REPO_ROOT / "data" / "library_sources"


def _image_roots() -> list:
    """BOTH directories a picture the household sent to Puff can be in.

    The Send-to-Puff path saves a file into the uploads directory, but a
    document that went through the knowledge screen has its original kept in
    ``data/library_sources/<catalog_id>.<ext>`` instead — which on the live
    box is where every one of the household's photos actually is, while the
    uploads directory sits empty. Listing one root was the whole bug.
    """
    roots = []
    for resolver in (_uploads_dir, _library_sources_dir):
        try:
            root = Path(resolver())
        except Exception as exc:    # pragma: no cover - defensive
            logger.debug("image root unavailable: %s", exc)
            continue
        if root not in roots:
            roots.append(root)
    return roots


def _upload_mime(name: str) -> str | None:
    return _EXT_MIME.get(os.path.splitext(str(name).lower())[1])


def _catalog_id(name: str, catalog: dict) -> str:
    """The catalog key for an on-disk file, or the filename when unknown.

    Uploads are stored under their catalog id verbatim; library sources are
    stored as ``<catalog_id><ext>``. Trying both is what lets the same
    picture in two roots collapse to one row.
    """
    if name in catalog:
        return name
    stem = os.path.splitext(name)[0]
    if stem in catalog:
        return stem
    return name


def _upload_catalog() -> dict:
    """Doc metadata by id, from the SAME set ``/ingest/list`` reports.

    ``upload_index.all_uploaded_docs`` unions the ingestion catalog with the
    knowledge-upload manifest; the ingestion catalog alone is empty on a box
    whose uploads all went through the knowledge screen, which is exactly
    where the pictures are — so asking it alone gave every row a fallback
    filename and an mtime instead of the name the household typed.
    """
    catalog = {}
    try:
        from puffpai.upload_index import all_uploaded_docs
        docs = all_uploaded_docs()
    except Exception as exc:        # pragma: no cover - defensive
        logger.debug("upload catalog unavailable: %s", exc)
        try:
            from puffpai.ingestion import list_ingested_docs
            docs = list_ingested_docs()
        except Exception:
            return catalog
    for doc in docs or ():
        try:
            if doc.get("id"):
                catalog.setdefault(str(doc["id"]), doc)
        except AttributeError:      # pragma: no cover - defensive
            continue
    return catalog


def upload_images() -> list:
    """Every IMAGE the household has sent to Puff, newest first.

    Read from the directories rather than the text catalog: the catalog holds
    extracted text (an image with no OCR text may not be in it at all), and
    what this list is for is picking a picture. Both roots are walked (see
    :func:`_image_roots`) and the same picture found in both is listed once.
    """
    catalog = _upload_catalog()
    rows = []
    seen = set()
    for directory in _image_roots():
        try:
            entries = list(directory.iterdir())
        except (OSError, AttributeError):
            continue
        for entry in entries:
            if not entry.is_file():
                continue
            mime = _upload_mime(entry.name)
            if mime is None:
                continue
            try:
                stat = entry.stat()
            except OSError:         # pragma: no cover - races
                continue
            key = _catalog_id(entry.name, catalog)
            if key in seen:
                continue
            seen.add(key)
            doc = catalog.get(key) or {}
            rows.append({
                "upload_id": entry.name,
                # The saved name is "<epoch_ms>_<original>"; show the
                # original, and the catalog's own filename when it has one.
                "filename": doc.get("filename") or _original_name(entry.name),
                # What to SHOW: the household's own name for the picture, or
                # Puff's label, falling back to the filename.
                "title": (doc.get("title") or doc.get("filename")
                          or _original_name(entry.name)),
                "mime": mime,
                "size": stat.st_size,
                "uploaded_at": doc.get("indexed_at") or datetime.fromtimestamp(
                    stat.st_mtime).isoformat(),
                "thumb_url": f"/ingest/images/{entry.name}/thumb",
                "_mtime": stat.st_mtime,
            })
    rows.sort(key=lambda r: (r["_mtime"], r["uploaded_at"]), reverse=True)
    for row in rows:
        row.pop("_mtime", None)
    return rows


def _original_name(file_id: str) -> str:
    head, _, tail = str(file_id).partition("_")
    return tail if head.isdigit() and tail else str(file_id)


def resolve_upload_image(upload_id: str) -> Path:
    """The on-disk file for *upload_id*, proven to be inside an image root.

    Refuses traversal (``../``, absolute paths, symlinks out of the tree) and
    anything that is not an image — this path takes a caller-supplied id and
    turns it into a file read, which is exactly the shape that leaks files.
    The containment check is per root and unchanged: the resolved path's
    parent must BE one of the roots.
    """
    raw = str(upload_id or "").strip()
    if not raw:
        raise HouseholdError("upload_not_found", "No upload id given.")
    found = None
    for directory in _image_roots():
        try:
            root = directory.resolve()
            resolved = (root / raw).resolve()
        except OSError:             # pragma: no cover - defensive
            continue
        if resolved.parent != root or not resolved.is_file():
            continue
        found = resolved
        break
    if found is None:
        raise HouseholdError("upload_not_found", "No such upload.")
    if _upload_mime(found.name) is None:
        raise HouseholdError("not_an_image", "That upload isn't an image.")
    return found


def avatar_from_upload(member_id: str, upload_id: str) -> dict:
    """Make *member_id*'s avatar out of a file the household sent to Puff."""
    path = resolve_upload_image(upload_id)
    data = path.read_bytes()
    return save_avatar(member_id, data)


def upload_thumb(upload_id: str) -> Path:
    """A 256 px JPEG of an upload, generated once and cached."""
    source = resolve_upload_image(upload_id)
    target = thumb_path(source.name)
    try:
        if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
            return target
    except OSError:                 # pragma: no cover - races
        pass
    data = source.read_bytes()
    if sniff_image(data) is None:
        raise HouseholdError("not_an_image", "That upload isn't an image.")
    try:
        thumb = _square_jpeg(data, THUMB_PX)
    except Exception as exc:
        logger.info("thumb decode failed (%s); serving the file as-is", exc)
        thumb = None
    if thumb is None:
        return source
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(thumb)
    return target


__all__ = [
    "HouseholdError", "ROLES", "OWNER_PLACEHOLDER",
    "members", "names", "owner_id", "get_member", "create_member",
    "update_member", "delete_member", "set_owner_name", "fitfam_keys_for",
    "sync_household_consumers", "slug",
    "avatars_dir", "avatar_path", "save_avatar", "delete_avatar",
    "sniff_image", "upload_images", "upload_thumb", "avatar_from_upload",
    "resolve_upload_image",
]
