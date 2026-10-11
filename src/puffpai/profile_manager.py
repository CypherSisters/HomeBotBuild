"""
PuffPAI Profile & Identity Manager
===================================
Handles user profiles and PuffPAI AI identity persistence.

Data files:
  profiles.json       — user profile list. Runtime STATE: lives in
                        config_dir(), which follows $PUFFPAI_DATA_DIR.
  puffpai_config.json — AI identity, merged into the existing config. This is
                        CONFIG, not state: always <repo>/config, never
                        relocated. See config_path() for why.
"""

import os
import json
import uuid
from datetime import datetime
from pathlib import PurePath

from puffpai.statedir import resolve_runtime_dir

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Historical location — <repo>/config, read-only on a hardened unit, so both
#: writes below failed there. Only profiles.json follows $PUFFPAI_DATA_DIR out
#: of it (keeping the "config" dir name under the data root);
#: puffpai_config.json stays put — see config_path().
_DEFAULT_CONFIG_DIR = os.path.join(_REPO_ROOT, "config")

# The three path names below stay READABLE as module attributes, but they are
# no longer literals: a literal would have to be built at import time, and
# resolving at import is what statedir.py exists to prevent. They are served
# from __getattr__ instead (PEP 562), which only fires when the name is absent
# from the module globals -- so an explicit assignment still wins as an
# override. Clearing any leftover on (re)import keeps a reload a full reset,
# exactly as it was when these were plain constants.
for _name in ("_CONFIG_DIR", "_PROFILES_PATH", "_CONFIG_PATH"):
    globals().pop(_name, None)
del _name


def _override(name: str) -> str | None:
    """Return an explicitly assigned module-level path, if there is one.

    str or Path both count -- silently ignoring a Path-valued override and
    resolving around it would write profiles somewhere the operator did not
    ask for. Nothing else does: a bare ``MagicMock`` satisfies os.fspath (it
    answers ``__fspath__`` with a junk name), and honouring that would scatter
    profile state to a junk path relative to the CWD.
    """
    value = globals().get(name)
    if not isinstance(value, (str, PurePath)):
        return None
    return os.fspath(value) or None


def config_dir() -> str:
    """Directory holding profiles.json — the state half of this module.

    Resolved per call, never at import: :mod:`puffpai.statedir` reaches
    ``puffpai.config`` (and thus the secrets provider) at call time.
    puffpai_config.json does NOT live here once $PUFFPAI_DATA_DIR is set.
    """
    return (_override("_CONFIG_DIR")
            or resolve_runtime_dir(_DEFAULT_CONFIG_DIR, env_subdir="config"))


def profiles_path() -> str:
    """Absolute path of profiles.json (runtime state; follows the env var)."""
    return (_override("_PROFILES_PATH")
            or os.path.join(config_dir(), "profiles.json"))


def config_path() -> str:
    """Absolute path of puffpai_config.json — wherever the LOADER reads it.

    Identity is CONFIG, owned by ``puffpai.config``'s read path (only
    profiles.json is state), and this module both reads and WRITES it — so the
    one rule that has to hold is that it names the same file the loader does.
    It used to name ``<repo>/config`` unconditionally, on the grounds that
    ``_build_config`` did too. It no longer does: ``config._config_dir_for``
    honours ``$PUFFPAI_CONFIG_DIR`` and relocates a non-dev install's config
    beside its state (seeded from the shipped copy). Keeping the literal here is
    now the bug the old docstring warned about, facing the other way —
    ``set_identity()`` would write the tracked shipped file while the appliance
    read the relocated one and went on answering to the old name.

    A dev checkout with in-tree state still resolves to ``<repo>/config``, so
    nothing moves for a developer. The ``_CONFIG_PATH`` override still wins, for
    tests and ops.
    """
    override = _override("_CONFIG_PATH")
    if override:
        return override
    try:
        from puffpai.config import resolve_config_dir
        return os.path.join(resolve_config_dir(), "puffpai_config.json")
    except Exception:
        return os.path.join(_DEFAULT_CONFIG_DIR, "puffpai_config.json")


def __getattr__(name: str) -> str:
    """Serve the three path names as resolved values (PEP 562).

    No in-repo caller imports these names anymore (api_server's
    /fitness/program endpoint now calls ``profiles_path()`` directly); the
    shim stays so a by-value ``from puffpai.profile_manager import
    _PROFILES_PATH`` still yields a resolved, env-aware path for anything out
    of tree that kept the old spelling. The accessors above stay the primary
    API; this only delegates to them, so there is one resolution path.

    TESTS: never ``monkeypatch.setattr`` these three names. Its undo assigns
    the value it read back as a real global -- and what it read is the value
    THIS function synthesized -- which pins that path for the rest of the
    session. Use ``monkeypatch.setitem(vars(module), name, value)`` or
    ``mock.patch`` (both delete the name again on exit).
    """
    if name == "_CONFIG_DIR":
        return config_dir()
    if name == "_PROFILES_PATH":
        return profiles_path()
    if name == "_CONFIG_PATH":
        return config_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

#: Roster fields :mod:`puffpai.household` keeps on a profile. Listed here
#: rather than inline so the create and the update paths cannot drift: a field
#: this module refuses to store is a field the household roster silently loses.
PROFILE_EXTRA_FIELDS = frozenset({"role", "emoji", "color", "fitfam_key"})

_DEFAULT_IDENTITY = {
    "name": "PuffPAI",
    "avatar_style": "adventurer",
    "avatar_seed": "PuffPAI",
}


class ProfileManager:
    """Manages user profiles and PuffPAI identity config."""

    def __init__(self):
        # Resolve once per instance so a read and the write that follows it can
        # never land in different directories mid-session. The two now live in
        # DIFFERENT directories on a relocated box (state vs config), so each
        # write site makes its own parent; only the state dir is made here.
        # Never the config dir: it is read-only on a hardened unit, and failing
        # to construct a ProfileManager would take the whole profile UI down
        # over a file we only sometimes write.
        self._profiles_path = profiles_path()
        self._config_path = config_path()
        parent = os.path.dirname(self._profiles_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._profiles = []
        self._last_active_profile = None
        self._identity = dict(_DEFAULT_IDENTITY)
        self._load()

    # ── File I/O ───────────────────────────────────────────────────────────

    def _load(self):
        """Load profiles and identity from disk."""
        # Profiles
        if os.path.exists(self._profiles_path):
            try:
                with open(self._profiles_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._profiles = data.get("profiles", [])
                self._last_active_profile = data.get("last_active_profile")
            except Exception as e:
                print(f"[ProfileManager] Failed to load profiles: {e}")
                self._profiles = []

        # Identity from main config
        if os.path.exists(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                identity = config.get("identity", {})
                for key in _DEFAULT_IDENTITY:
                    if key in identity:
                        self._identity[key] = identity[key]
            except Exception as e:
                print(f"[ProfileManager] Failed to load identity: {e}")

    def _save_profiles(self):
        """Write profiles list to profiles.json."""
        data = {
            "profiles": self._profiles,
            "last_active_profile": self._last_active_profile,
        }
        os.makedirs(os.path.dirname(self._profiles_path) or ".", exist_ok=True)
        with open(self._profiles_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _save_identity(self):
        """Merge identity block into puffpai_config.json (preserves other keys)."""
        config = {}
        if os.path.exists(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception:
                pass
        config["identity"] = dict(self._identity)
        os.makedirs(os.path.dirname(self._config_path) or ".", exist_ok=True)
        with open(self._config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

    # ── Profile CRUD ───────────────────────────────────────────────────────

    def get_all_profiles(self):
        """Return profiles sorted by last_active descending."""
        return sorted(
            self._profiles,
            key=lambda p: p.get("last_active", ""),
            reverse=True,
        )

    def get_profile(self, profile_id):
        """Look up a single profile by id. Returns dict or None."""
        for p in self._profiles:
            if p["id"] == profile_id:
                return p
        return None

    def create_profile(self, display_name, avatar_style="adventurer",
                       avatar_seed=None, profile_id=None, extra=None):
        """Create and persist a new profile. Returns the profile dict.

        ``profile_id`` lets the caller choose the id (the household
        roster keys members by a slug of their name, so the id that appears in
        the Calendar's family list and in FitFam's ``users`` is the same
        readable string everywhere). ``extra`` carries the roster fields —
        role, emoji, colour — set at creation rather than in a second write.
        """
        profile = {
            "id": profile_id or f"usr_{uuid.uuid4().hex[:8]}",
            "display_name": display_name,
            "avatar_style": avatar_style,
            "avatar_seed": avatar_seed or display_name,
            "created": datetime.now().isoformat(),
            "last_active": datetime.now().isoformat(),
        }
        for key, value in (extra or {}).items():
            if key in PROFILE_EXTRA_FIELDS:
                profile[key] = value
        self._profiles.append(profile)
        self._save_profiles()
        return profile

    def update_profile(self, profile_id, **kwargs):
        """Update fields on an existing profile. Allowed: display_name,
        avatar_style, avatar_seed."""
        profile = self.get_profile(profile_id)
        if not profile:
            return
        allowed = {"display_name", "avatar_style", "avatar_seed"} | PROFILE_EXTRA_FIELDS
        for key, val in kwargs.items():
            if key in allowed:
                profile[key] = val
        self._save_profiles()

    def delete_profile(self, profile_id):
        """Remove a profile (does NOT delete its chat history)."""
        self._profiles = [p for p in self._profiles if p["id"] != profile_id]
        if self._last_active_profile == profile_id:
            self._last_active_profile = None
        self._save_profiles()

    def set_last_active(self, profile_id):
        """Mark a profile as most recently used."""
        profile = self.get_profile(profile_id)
        if profile:
            profile["last_active"] = datetime.now().isoformat()
            self._last_active_profile = profile_id
            self._save_profiles()

    def get_last_active_id(self):
        """Return the id of the most recently used profile, or None."""
        return self._last_active_profile

    # ── PuffPAI Identity ───────────────────────────────────────────────────

    def get_identity(self):
        """Return identity dict: {name, avatar_style, avatar_seed}."""
        return dict(self._identity)

    def set_identity(self, name=None, avatar_style=None, avatar_seed=None):
        """Update identity fields and persist."""
        if name is not None:
            self._identity["name"] = name
        if avatar_style is not None:
            self._identity["avatar_style"] = avatar_style
        if avatar_seed is not None:
            self._identity["avatar_seed"] = avatar_seed
        self._save_identity()
