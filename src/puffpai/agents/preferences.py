"""Per-build agent preferences — display names and feature toggles.

Two things a person building their own PuffPAI should be able to change without
root, without a rebuild, and without touching code:

* **What the agents are called.** The internal ids (``concierge``,
  ``steward``, …) are load-bearing — the bus routes on them, the Sentinel
  reviewer is wired by name, and the credential scopes share the spelling. They
  never change. What a household *sees* is a separate, purely cosmetic layer,
  so renaming the team can never reach the veto.
* **Feature toggles** like the planning loop. Kept only in an environment
  variable set in a root-owned systemd file, the owner of the appliance could
  not turn their own feature on.

Both read from the ``agents`` section of ``config/puffpai_config.json``:

.. code-block:: json

    "agents": {
      "planning_loop_enabled": true,
      "name_preset": "default",
      "names": {"caretaker": "Housekeeper"},
      "egress_allowlist": ["example.com"]
    }

``names`` overrides individual agents on top of the chosen preset, so a build
can take a preset and change one name without restating the rest.

**Environment always wins** over config for toggles. An operator debugging a
misbehaving appliance must be able to force a feature off from the unit file
without editing (or racing) a JSON file the running service also reads.
:func:`egress_allowlist` is the one deliberate exception, for the reason stated
at that function: it is the only setting here that can *widen* what the
appliance reaches, and environment-wins would add a second place to do it.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import ipaddress
import json
import os
import threading

from puffpai.logging_config import get_logger

logger = get_logger(__name__)

#: Canonical internal ids. This tuple is the contract; presets are keyed by it.
AGENT_IDS = ("concierge", "steward", "sentinel", "archivist", "majordomo",
             "caretaker", "toolsmith")

#: Shipped name sets. ``default`` is the role vocabulary the docs use.
PRESETS: dict[str, dict[str, str]] = {
    "default": {
        "concierge": "Concierge",
        "steward": "Steward",
        "sentinel": "Drawbridge",
        "archivist": "Archivist",
        "majordomo": "Majordomo",
        "caretaker": "Caretaker",
        "toolsmith": "Toolsmith",
    },
}

DEFAULT_PRESET = "default"

#: Cosmetic, but it still reaches a screen and a text-to-speech voice: cap the
#: length and strip control characters so a hand-edited config cannot produce a
#: broken dashboard or a name that spans the whole tile.
MAX_NAME_LEN = 40

_lock = threading.Lock()
_cache: dict | None = None


def _config_path() -> str | None:
    """Absolute path to puffpai_config.json, or ``None`` if unavailable.

    Resolved through :func:`puffpai.config.resolve_config_dir`, the same rule
    the rest of the build uses, because :func:`set_names` WRITES this file. With
    the old ``<repo>/config`` literal, ``PUT /agents/names`` on any non-dev
    install (``$PUFFPAI_CONFIG_DIR`` or ``$PUFFPAI_DATA_DIR`` set) rewrote the
    shipped, tracked config inside the checkout instead of the owner's own copy
    — a rename showed up as a dirty git file, and on a read-only code mount it
    could not be saved at all. A dev checkout that already holds its state
    in-tree still resolves to ``<repo>/config``, so nothing moves for a
    developer; a relocated dir is seeded from the shipped file, so the first
    read after the move sees the shipped values.
    """
    try:
        from puffpai.config import resolve_config_dir
        return os.path.join(resolve_config_dir(), "puffpai_config.json")
    except Exception as exc:  # preferences must never gate startup
        logger.debug("config path unavailable for agent preferences: %s", exc)
        return None


def _agents_section() -> dict:
    """The ``agents`` block, read once and cached. Never raises.

    Read directly rather than through a config dataclass because unknown keys
    are dropped there, and this section must stay open-ended: a build may add
    its own names without the loader knowing about them in advance.
    """
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        section: dict = {}
        path = _config_path()
        if path:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                candidate = raw.get("agents")
                if isinstance(candidate, dict):
                    section = candidate
            except (OSError, ValueError) as exc:
                logger.debug("agent preferences unreadable (%s): %s", path, exc)
        _cache = section
        return section


def reset_cache() -> None:
    """Forget the cached section (tests, and a reload after an edit)."""
    global _cache
    with _lock:
        _cache = None


# ── feature toggles ─────────────────────────────────────────────────

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def flag(env_var: str, config_key: str, default: bool = False) -> bool:
    """Resolve a toggle: environment first, then config, then *default*.

    Environment wins so an operator can force a feature off from the unit file
    without editing the JSON the running service also reads.
    """
    raw = os.environ.get(env_var)
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in _TRUE
    value = _agents_section().get(config_key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in _TRUE + _FALSE:
        return value.strip().lower() in _TRUE
    return default


# ── the owner's egress allow-list ───────────────────────────────────

#: More hosts than a household appliance has any cause to reach, and small
#: enough that the whole set fits legibly in the audit line the planning loop
#: writes for every run.
MAX_ALLOWLIST_ENTRIES = 64
#: The DNS limit. An entry longer than this is not a hostname.
MAX_HOSTNAME_LEN = 253

#: Characters that mean "this is a URL, not a hostname". An entry containing any
#: of them is **refused**, never parsed: quietly taking the host out of
#: ``https://evil.example/@good.example`` is how an allow-list ends up
#: authorising a host the owner never read. Refusing makes the owner fix the
#: entry; parsing would make the appliance guess what they meant.
_NOT_IN_A_HOSTNAME = frozenset("/:@?#\\%,\"'<>[]{}|^`&=+;*!$()~ \t\r\n\v\f\x00")

#: What is left after that: the LDH set plus the label separator. Deliberately
#: ASCII-only — a unicode entry cannot match anything, because the hosts this is
#: compared against come out of ``urlsplit`` on a URL the Toolsmith has already
#: shape-checked, and allowing homographs into a security list buys nothing.
_HOSTNAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-.")


def egress_allowlist() -> tuple[str, ...]:
    """Hostnames this build's owner has pre-approved for outbound fetches.

    Read from ``agents.egress_allowlist``: a list of **bare hostnames**, e.g.
    ``["example.com", "weather.example.org"]``. The planning loop unions this
    with the hosts the human named in the goal to form the frozen scope a run
    may reach (see :class:`puffpai.agents.planning_loop.EgressScope`).

    **Config only — there is deliberately no environment override**, unlike
    :func:`flag`. Environment-wins is the right rule for a toggle an operator
    needs to force *off* from a root-owned unit file. An allow-list can only
    ever *widen* what the appliance may reach, and an operator who needs to stop
    outbound work already can: the planning-loop kill-switch does it. Keeping
    the number of places that can widen egress at exactly one is worth the small
    inconsistency with the rest of this module.

    Fails closed in every direction. A missing key, a non-list, an entry that is
    really a URL, an IP literal, a name with no dot, an over-long entry, or junk
    types all drop that entry (or the whole list) and yield fewer hosts — never
    an exception, and never a wildcard.
    """
    raw = _agents_section().get("egress_allowlist")
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        logger.warning(
            "agents.egress_allowlist must be a list of hostnames, got %s; "
            "ignoring it (no host is allow-listed)", type(raw).__name__)
        return ()

    hosts: set[str] = set()
    for entry in list(raw)[:MAX_ALLOWLIST_ENTRIES]:
        host = _clean_hostname(entry)
        if host:
            hosts.add(host)
    if len(raw) > MAX_ALLOWLIST_ENTRIES:
        logger.warning("agents.egress_allowlist has %d entries; only the first "
                       "%d are used", len(raw), MAX_ALLOWLIST_ENTRIES)
    return tuple(sorted(hosts))


def _clean_hostname(entry) -> str:
    """One allow-list entry, normalised. ``""`` means 'refuse this entry'."""
    if not isinstance(entry, str):
        logger.warning("agents.egress_allowlist entry is not a string (%s); "
                       "ignoring it", type(entry).__name__)
        return ""
    host = entry.strip().lower().rstrip(".")
    if not host:
        return ""
    if len(host) > MAX_HOSTNAME_LEN:
        logger.warning("agents.egress_allowlist entry exceeds %d characters; "
                       "ignoring it", MAX_HOSTNAME_LEN)
        return ""
    if any(ch in _NOT_IN_A_HOSTNAME for ch in host):
        logger.warning("agents.egress_allowlist wants a bare hostname, not a "
                       "URL or a path; ignoring an entry containing a "
                       "delimiter")
        return ""
    if set(host) - _HOSTNAME_CHARS:
        logger.warning("agents.egress_allowlist entry has characters that "
                       "cannot appear in an ASCII hostname; ignoring it")
        return ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # Names only. A literal would authorise nothing the Toolsmith's
        # resolved-address checks do not already decide, and it makes the audit
        # line ("which sites is this build allowed to read?") unreadable.
        logger.warning("agents.egress_allowlist takes hostnames, not IP "
                       "literals; ignoring an address entry")
        return ""
    if "." not in host:
        # Stricter than the spec strictly needs, and on purpose: a single-label
        # entry is either a typo or something like ``localhost``, and neither
        # should be able to appear in a list whose only job is to open sockets.
        logger.warning("agents.egress_allowlist entry has no dot; ignoring it")
        return ""
    return host


# ── display names ───────────────────────────────────────────────────

def _clean(value, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = " ".join(
        "".join(ch if ch.isprintable() else " " for ch in value).split())
    return cleaned[:MAX_NAME_LEN] or fallback


def display_names() -> dict[str, str]:
    """Every agent id mapped to what this build calls it.

    Always returns a name for every id in :data:`AGENT_IDS`, whatever the
    config says — an unknown preset, a partial ``names`` map, or a junk value
    all fall back to the role name rather than leaving a blank on screen.
    """
    section = _agents_section()
    preset_name = section.get("name_preset")
    preset = PRESETS.get(str(preset_name).strip().lower()) if preset_name else None
    if preset_name and preset is None:
        logger.warning("unknown agent name_preset %r; using %r",
                       preset_name, DEFAULT_PRESET)
    names = dict(PRESETS[DEFAULT_PRESET])
    names.update(preset or {})

    overrides = section.get("names")
    if isinstance(overrides, dict):
        for agent_id, value in overrides.items():
            key = str(agent_id).strip().lower()
            if key in names:  # only real agent ids; never invents an agent
                names[key] = _clean(value, names[key])
    return names


def display_name(agent_id: str) -> str:
    """What this build calls *agent_id* (the id itself if it is not an agent)."""
    return display_names().get(str(agent_id).strip().lower(), str(agent_id))


def available_presets() -> list[str]:
    """Preset names a build can choose (for docs and the settings UI)."""
    return sorted(PRESETS)


# ── renaming the team from the outside ──────────────────────────────
#
# Reading these names has always worked; nothing could WRITE them, so "rename
# your agents" meant hand-editing a JSON file the running service also reads.
# The write side lives here, next to the reader, for one reason: the reader
# caches the section, and a writer anywhere else would leave that cache stale.

#: Cap for a name a person TYPES. Tighter than :data:`MAX_NAME_LEN` (which
#: bounds a hand-edited config) because this one has to fit a dashboard tile
#: and be spoken aloud: 24 characters is longer than every shipped name.
MAX_SET_NAME_LEN = 24

#: Names an agent may not be given. Two kinds, one rule — a display name must
#: not be able to claim to be something it is not:
#:
#: * the product and its builds ("Puff", "HomeBot", …), because the household
#:   reads Puff's own words next to the agent's and a tile labelled "PuffPAI"
#:   is the appliance impersonating itself;
#: * the privileged roles ("owner", "admin", "root", "system"), because the
#:   consent and PIN prompts name who is asking.
#:
#: Compared against the normalised (lowercased, whitespace-collapsed) name, so
#: "  PuffPAI " is refused too. This is cosmetic-layer hygiene, not a security
#: control: the internal ids are what anything load-bearing routes on.
RESERVED_NAMES = frozenset({
    "puff", "puffpai", "puff pai", "homebot", "buildbot",
    "cypher sisters", "cyphersisters",
    "owner", "admin", "administrator", "root", "system", "sudo",
})


class NameRefused(ValueError):
    """A display name this build will not store, with the reason to show."""


def clean_display_name(value) -> str:
    """The stored form of a typed name: printable, collapsed, bounded."""
    if not isinstance(value, str):
        return ""
    return " ".join(
        "".join(ch if ch.isprintable() else " " for ch in value).split()
    )[:MAX_SET_NAME_LEN]


def check_name(value) -> str | None:
    """``None`` when *value* is storable, else the sentence to show the owner."""
    if not isinstance(value, str):
        return "A name has to be text."
    cleaned = clean_display_name(value)
    if not cleaned:
        return "A name needs at least one printable character."
    if len(" ".join(value.split())) > MAX_SET_NAME_LEN:
        return f"Names are at most {MAX_SET_NAME_LEN} characters."
    if cleaned.lower() in RESERVED_NAMES:
        return (f"'{cleaned}' is reserved — it names the appliance itself or "
                f"a privileged role, so an agent cannot be called it.")
    return None


def _write_agents_section(section: dict) -> None:
    """Replace the ``agents`` block in puffpai_config.json, atomically.

    A raw-JSON edit (the same idiom as :mod:`puffpai.hatch`): every other key
    in the file — and every key in this section the loader does not know about
    — survives the write, and a crash mid-write cannot truncate the appliance's
    main config file.
    """
    path = _config_path()
    if not path:
        raise NameRefused("This build has no config file to save names in.")
    raw: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            raw = loaded
    except (OSError, ValueError) as exc:
        logger.debug("config unreadable before name write (%s): %s", path, exc)
    raw["agents"] = section
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def set_names(preset=None, names=None) -> dict[str, str]:
    """Persist a preset and/or per-agent overrides. Returns the new names.

    Neither argument is required; passing neither is a no-op read. An override
    for an id that is not in :data:`AGENT_IDS` is refused rather than stored —
    a config full of names for agents that do not exist is how "rename the
    team" turns into "why is nothing renamed". ``None`` as a name CLEARS that
    override, so an owner can go back to the preset's word.

    Raises :class:`NameRefused` with a showable sentence; writes nothing when
    it does.
    """
    section = dict(_agents_section())

    if preset is not None:
        key = str(preset).strip().lower()
        if key not in PRESETS:
            raise NameRefused(
                f"'{preset}' is not a name set this build carries "
                f"({', '.join(available_presets())}).")
        section["name_preset"] = key

    if names is not None:
        if not isinstance(names, dict):
            raise NameRefused("Names must be given as {agent id: name}.")
        current = section.get("names")
        overrides = dict(current) if isinstance(current, dict) else {}
        for agent_id, value in names.items():
            key = str(agent_id).strip().lower()
            if key not in AGENT_IDS:
                raise NameRefused(
                    f"'{agent_id}' is not one of this build's agents "
                    f"({', '.join(AGENT_IDS)}).")
            if value is None:
                overrides.pop(key, None)
                continue
            problem = check_name(value)
            if problem:
                raise NameRefused(problem)
            overrides[key] = clean_display_name(value)
        section["names"] = overrides

    _write_agents_section(section)
    reset_cache()
    return display_names()


__all__ = [
    "AGENT_IDS",
    "DEFAULT_PRESET",
    "MAX_ALLOWLIST_ENTRIES",
    "MAX_HOSTNAME_LEN",
    "MAX_NAME_LEN",
    "MAX_SET_NAME_LEN",
    "NameRefused",
    "PRESETS",
    "RESERVED_NAMES",
    "available_presets",
    "check_name",
    "clean_display_name",
    "display_name",
    "display_names",
    "egress_allowlist",
    "flag",
    "reset_cache",
    "set_names",
]
