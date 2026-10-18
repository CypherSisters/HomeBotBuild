"""App manifests — an app is a folder with a declaration, not a core edit.

The same app contract as BuildBot ("an app is a folder"), so the two builds
read the SAME ``app.json`` files. An app written for one runs on the other;
that is the whole point of the contract and the reason nothing here is
HomeBot-shaped.

Without a manifest, an app is a pattern hand-copied through the core: the
valid-app list and default data shape in :mod:`puffpai.app_data`, the dashboard
entry, the capability record, and - the sharpest one - the words the router
knows for it. "Install an app" becomes "fork the core", a build with no Trivia
app still has trivia vocabulary, and a NEW app gets no vocabulary at all.

This module is the other end of that. ``apps/<id>/app.json`` declares, in one
place, everything the core needs to know, and installing is rescanning: no
script runs, nothing is imported, nothing is executed.

**A manifest is data and only data.** It is parsed as JSON, validated, and used
as lists and strings. No field is ever imported, resolved to a module path,
``eval``'d, or concatenated into the model's instructions. A third-party app is
untrusted input, and the routing layer being keyword-based rather than
prompt-injected tool definitions is what makes a hostile app's vocabulary
harmless: it can add words to a matcher, and that is all it can do. Do not
"improve" routing by feeding app declarations to the model as text. The one
exception is narrow and does not come from a manifest: for a browser app
Toolsmith added (``kind: "web"``), the action words Toolsmith's
compiler made from the app itself and Drawbridge checked (R1-R12, sealed, see
:mod:`puffpai.webapps.recipes`) may be shown to the model as tool
descriptions. A web manifest itself still carries no words for the router and
no actions.

A declared surface is a claim of membership, not a grant of capability. Naming
``house.cameras`` says which existing surface this app belongs to so the gate can
reason about it; it does not produce camera access. Vault, consent, action gates
and egress remain contracts no manifest field touches. The same holds for the
privacy block (:func:`_validate_privacy`): a declared door is a claim about
where an app wants to reach, never permission to reach there.

A malformed manifest is recorded (see :func:`load_problems`) and skipped, the
way a corrupt config file is: the other apps keep working and the owner is told,
rather than quietly running a build that is missing something.

Three differences from BuildBot, all forced by this tree and none of them
loosening anything:

* BuildBot records manifest faults through ``puffpai.config_health``, which this
  build does not carry. Faults are logged and kept in a module-level list that
  :func:`load_problems` hands to the API, so the owner can still be told.
* BuildBot reads the surface taxonomy from its capability catalog via
  ``build_interview.surface_taxonomy()``. That module is BuildBot-only, so the
  closed set is baked in here, byte-identical to BuildBot's fallback copy. If
  ``build_interview`` ever lands in this tree it wins automatically.
* Packs (:data:`PACK_SCHEMA`) exist only here: an APP is one tool with its own
  manifest, a PACK is a curated bundle of apps.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

SCHEMA = "puffpai_app/v1"

#: Pack manifests. A pack is a curated BUNDLE of apps for a topic or persona;
#: it carries no data, no vocabulary and no code of its
#: own, and every app it lists must already be installed.
PACK_SCHEMA = "puffpai_pack/v1"

# The rulepack surface app vocabulary is assembled into. Apps contribute words
# to the router's existing "apps" pack; they do not invent router surfaces.
APPS_SURFACE = "apps"

APPS_DIRNAME = "apps"
MANIFEST_NAME = "app.json"

PACKS_DIRNAME = "packs"
PACK_MANIFEST_NAME = "pack.json"

CONFIG_NAME = "app_manifests"
PACK_CONFIG_NAME = "pack_manifests"

#: A SHIPPED app whose manifest says ``"first_app": true`` is one of the bot's
#: first apps: it ships turned off, and the owner (or the plan's Yes) turns it
#: on per bot (:mod:`puffpai.first_apps`). An app in the owner's own folder is
#: never off, whatever its manifest says.
FIRST_APP_KEY = "first_app"


# ---------------------------------------------------------------------------
# bounds
# ---------------------------------------------------------------------------
# Everything an app declares is bounded, because an app is untrusted input and
# the router is a linear scan over every keyword of every pack for every
# message. The numbers below are "comfortably more than any honest app needs,
# far less than enough to do damage":
#
#   * 64 KB per manifest - the five shipped manifests are all under 3 KB, and
#     the largest legitimate field (a default data shape) is bounded separately.
#   * 60 keywords per app - the busiest shipped app (calendar) declares 8. Sixty
#     apps at sixty words each is 3600 substring checks per message, which is
#     still cheap; six thousand words per app would not be.
#   * 2..64 characters per keyword - a one-character keyword matches nearly every
#     message, which is the flooding attack by another route.
#   * 12 examples of 120 characters - examples are published on the phrasebook
#     page, so this is a page-length bound as much as a safety one.
#   * 64 installed apps - past that the dashboard and the phrasebook stop being
#     readable, and no owner has curated that many.
#   * 32 KB of default data - a default is a starting shape, not a dataset. An
#     app that wants to ship content ships it as knowledge, under that gate.
MAX_MANIFEST_BYTES = 64 * 1024
MAX_INSTALLED_APPS = 64
MIN_ID_LEN = 2
MAX_ID_LEN = 32
MAX_KEYWORDS = 60
MIN_KEYWORD_LEN = 2
MAX_KEYWORD_LEN = 64
MAX_EXAMPLES = 12
MAX_EXAMPLE_LEN = 120
MAX_ACTIONS = 32
MAX_SURFACES = 8
MAX_LABEL_LEN = 200
MAX_DEFAULT_BYTES = 32 * 1024
MAX_ORDER = 9999

# Privacy block (Phase A, five fields — PUFFPAI_WHAT_GOOD_LOOKS_LIKE §1.3).
MAX_DOORS = 12
MAX_DOOR_HOSTS = 16
MAX_HOST_LEN = 253          # the DNS name-length ceiling
MAX_PURPOSE_LEN = 140       # "an author who cannot say why in one line…"
MAX_DATA_SENT_LEN = 200

# Packs.
MAX_INSTALLED_PACKS = 32
MAX_PACK_APPS = 64

# Second, tighter bounds for the strings that leave the box. `name` and `blurb`
# are validated at 200 characters on the way in, which is the right ceiling for
# a file on disk; a nav label at 200 characters is a broken sidebar. Bounding
# again on the way out means a manifest edited after install, or one loaded by
# an older validator, still cannot stretch the UI.
MAX_UI_NAME_LEN = 64
MAX_UI_BLURB_LEN = 200
MAX_UI_ICON_LEN = 64

# A route that leaves the box is an href the browser will follow. Validation
# already refuses '..' and anything not absolute; this refuses the rest of what
# an absolute-looking string can still be. '//evil.example' starts with a slash
# and is a protocol-relative URL off this box, and a colon opens the door to a
# scheme. Neither has any business in an in-app route.
_SAFE_ROUTE_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")

# Lowercase, starts with a letter, letters/digits/underscore after. The id
# becomes a path component and a data filename, so it is the single most
# dangerous field in the manifest: no dots, no separators, no case games, no
# leading underscore. It is still passed through a containment check before it
# touches a path - a pattern is a first line of defence, not the only one.
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# The id Toolsmith issues for an app it added from outside
# (``integrate.records.new_id``): ``<slug, <=30>-<8 random base32>``. The same
# rule as :data:`puffpai.webapps.ID_RE` and ``integrate.records.ID_RE``; one
# rule, so a browser app's manifest is never "not usable" here. Also safe as a
# path component: lowercase letters, digits and hyphens, no dot, no leading
# hyphen. A declarative app's id can never have this shape (no hyphen).
ISSUED_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,29}-[a-z2-7]{8}\Z")

#: A browser app's manifest (written by ``puffpai.webapps.install``) may hold
#: exactly these keys; anything else (words for the router, actions, a data
#: block, a feed, outside services) is refused.
WEB_KIND = "web"
_WEB_KEYS = frozenset({"schema", "id", "name", "blurb", "kind", "web", "outside", "ui"})
_WEB_SOURCE_KEYS = frozenset({"repo", "commit", "license", "sha256"})
MAX_WEB_NAME_LEN = 64
MAX_WEB_OUTSIDE = 50

# An exact hostname. A '*' is a VALIDATOR ERROR and not a silently dead door:
# wildcard hosts fail as a mystery at request time today, and the one field the
# box can independently check must mean exactly what it says.
_HOST_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
                      r"(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$")

# The last label of a registrable NAME: alphabetic, or the `xn--` ACE form of
# an internationalised TLD. Either way it BEGINS WITH A LETTER, which is the
# load-bearing property - no IP literal, in any base or shorthand, ends in a
# label that starts with a letter.
_TLD_RE = re.compile(r"^(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{1,59})$")


def _is_ip_literal(host: Any) -> bool:
    """True if *host* is an address rather than a name, in ANY notation.

    `_HOST_RE` alone accepted four of these, because a dotted quad is a legal
    hostname shape: a manifest declaring `{"hosts": ["8.8.8.8"]}` validated
    here and registered as a door. A door names a HOST, whose addresses the
    egress layer then resolves and judges against the address policy; a literal
    skips the name and points at an address directly.

    Catches every notation the platform resolver honours, not just the dotted
    quad: octal/zero-padded (`010.010.010.010`), hex (`0x08080808`), a bare
    integer (`2130706433` is 127.0.0.1), shorthand (`127.1`), and IPv6 in every
    form including bracketed and IPv4-mapped (`::1`, `[::1]`,
    `::ffff:127.0.0.1`).

    Mirrored in `puffpai.security.egress_doors._is_ip_literal`, which re-runs
    it at the enforcement point. Two copies on purpose: the loader refusing a
    manifest and the door layer refusing to register one are different controls
    and neither may rely on the other having run.
    """
    if not isinstance(host, str):
        return False
    text = host.strip()
    if not text:
        return False
    if text.startswith("[") or text.endswith("]") or ":" in text:
        return True
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        pass
    parts = text.split(".")
    if len(parts) > 4:
        return False
    for part in parts:
        if not part:
            return False
        low = part.lower()
        try:
            if low.startswith("0x"):
                int(low, 16)
            elif low.startswith("0") and len(low) > 1:
                int(low, 8)
            else:
                int(low, 10)
        except ValueError:
            return False
    return True


def _is_registrable_hostname(host: Any) -> bool:
    """True only for a NAME a door may declare. Refuses every address literal.

    Shape, then at least two labels (so a bare `localhost` or `2130706433` is
    never a door host), then a TLD-shaped last label - which is the single rule
    that kills every dotted numeric form, because `8.8.8.8` and `127.1` end in
    a label that does not start with a letter. The parser check runs as well:
    the rule is the reasoning, the parser is the proof.
    """
    if not isinstance(host, str) or not _HOST_RE.match(host):
        return False
    if _is_ip_literal(host):
        return False
    labels = host.split(".")
    if len(labels) < 2:
        return False
    return bool(_TLD_RE.match(labels[-1]))

#: What the box actually does with ``doors[].hosts[]`` TODAY, in one sentence,
#: carried on every payload that shows a door so no surface can imply more.
#:
#: A manifest's hosts are schema-checked at load (exact hostname, no
#: wildcard, no scheme), and :func:`register_egress_doors` runs on every
#: rescan and puts each manifest door in the table
#: :mod:`puffpai.security.egress_doors` enforces, as ``app:<id>:<n>``, forced
#: shut, with its declared hosts as an exact allowlist; the only sanctioned
#: outbound path for an app is ``egress_doors.app_egress_request``, which
#: refuses a shut door before DNS and an undeclared host before the socket.
#: So "enforced" is the true sentence.
#:
#: It is still a per-app FACT, not a constant claim: :func:`hosts_enforcement`
#: reports ``"declared_only"`` for any app whose doors did not actually make it
#: into the table (a malformed door block, or an egress module that would not
#: import). Surfaces read the per-record value, never this module-level one.
HOSTS_ENFORCEMENT = "enforced"
HOSTS_ENFORCEMENT_NOTICE = (
    "Registered as an egress door on your box: shut until you open it, and "
    "any host this app did not name is refused."
)

#: The label for an app whose declared doors are NOT in the enforced table, and
#: the sentence that goes with it. Byte-identical to what every app shipped
#: before the keystone, because for such an app nothing has changed.
HOSTS_DECLARED_ONLY = "declared_only"
HOSTS_DECLARED_ONLY_NOTICE = (
    "Declared by the author; your box does not yet enforce these hosts."
)

#: App ids whose every declared door reached the enforced table on the last
#: :func:`register_egress_doors`, and the set that was offered to it. Both are
#: replaced wholesale by that function, never mutated in place, so a reader
#: always sees one consistent generation.
_enforced_apps: set = set()
_declared_apps: set = set()

#: What a door's credential may be. `password` and `cookie` are refused
#: outright: an app that wants the owner's password is asking
#: for the one thing the vault exists to stop it having.
CREDENTIAL_KINDS = ("none", "api_key", "oauth2")
REFUSED_CREDENTIAL_KINDS = ("password", "cookie")

# v1 apps are declarative. These keys are the shapes a code-carrying app would
# take, and they are refused by name so the deferral is enforced rather than
# merely intended: an app that wants to run code is the next tier and needs a
# trust model that does not exist yet.
#
# The words here are REFUSALS, not vocabulary: nothing in this build is called a
# "plugin", and the entry exists so that a manifest calling itself one is turned
# away rather than half-understood.
_FORBIDDEN_KEYS = (
    "handler", "handlers", "module", "modules", "import", "imports",
    "entrypoint", "entry_point", "exec", "eval", "python", "script",
    "scripts", "command", "commands", "hook", "hooks", "plugin", "run",
)

#: The closed set of surfaces a build knows how to load, with labels. Byte-
#: identical to BuildBot's ``build_interview._FALLBACK_SURFACE_LABEL`` so one
#: manifest validates on both builds; see the module docstring.
_FALLBACK_SURFACE_LABEL = {
    "house.lights": "Lights and switches",
    "house.climate": "Thermostat and climate",
    "house.sensors": "Household sensors",
    "house.cameras": "Security cameras",
    "media.video": "Video library and playback",
    "media.audio": "Music and radio",
    "media.books": "Book and reading library",
    "knowledge.library": "Offline reference library",
    "knowledge.medical": "Medical reference",
    "knowledge.maps": "Maps and places",
    "instruments.telescope": "Telescope and sky feeds",
    "comms.calendar": "Calendar and events",
    "comms.timers": "Timers and alarms",
    "daily.kitchen": "Pantry and meals",
    "daily.fitness": "Fitness tracking",
    "daily.games": "Games and trivia",
    "daily.notes": "Notes and lists",
}


def surface_taxonomy() -> dict[str, str]:
    """Surface id -> label, for the surfaces a manifest may claim membership of.

    Prefers ``build_interview`` when a build carries it (BuildBot does), so the
    two trees never drift; falls back to the baked-in copy otherwise.
    """
    try:
        from puffpai import build_interview  # type: ignore
        raw = build_interview.surface_taxonomy()
        if isinstance(raw, dict) and raw:
            return {str(k): str((v or {}).get("label") or k)
                    if isinstance(v, dict) else str(v)
                    for k, v in raw.items()}
    except Exception:
        pass
    return dict(_FALLBACK_SURFACE_LABEL)


# ---------------------------------------------------------------------------
# core-owned bindings: which core tool / capability belongs to which app
# ---------------------------------------------------------------------------
# These maps live in the CORE, never in a manifest. A manifest naming the core
# tools it "owns" would be a grant dressed as a declaration - exactly what rule
# 2 of the contract forbids - so the direction is inverted: the core says which
# of ITS tools and capabilities stand or fall with which app, and the registry
# answers "is that app installed".
#
# An app id absent from these maps simply has no core tools; that is legitimate
# (a dashboard-only app) and is not an error.

#: assistant tool name -> the app whose data it reads or writes. Hand-kept and
#: checked against the capability registry by test_app_contract, which
#: fails if a tool named here stops existing.
CORE_TOOL_APPS = {
    # AlarmFlow
    "app_set_timer": "alarmflow",
    "app_set_alarm": "alarmflow",
    "app_list_alarms": "alarmflow",
    "app_dismiss_alarm": "alarmflow",
    "app_snooze_alarm": "alarmflow",
    "app_delete_alarm": "alarmflow",
    # Calendar - events, tasks, bills and medications are all one app's file
    "app_add_task": "calendar",
    "app_add_event": "calendar",
    "app_list_schedule": "calendar",
    "app_complete_task": "calendar",
    "app_edit_task": "calendar",
    "app_delete_task": "calendar",
    "app_edit_event": "calendar",
    "app_delete_event": "calendar",
    "app_add_bill": "calendar",
    "app_pay_bill": "calendar",
    "app_add_med": "calendar",
    "app_take_med": "calendar",
    # FreshTable
    "app_list_shopping": "freshtable",
    "app_add_shopping": "freshtable",
    "app_check_shopping": "freshtable",
    "app_list_pantry": "freshtable",
    "app_add_pantry": "freshtable",
    "app_remove_pantry": "freshtable",
    "app_list_recipes": "freshtable",
    "app_create_recipe": "freshtable",
    "auto_meal_plan": "freshtable",
    "meal_set": "freshtable",
    "meal_plan_show": "freshtable",
    "auto_grocery_list": "freshtable",
    # FitFam
    "app_fitness_status": "fitfam",
    "app_log_workout": "fitfam",
    "app_log_set": "fitfam",
    "app_personal_records": "fitfam",
    "app_generate_workout": "fitfam",
    "app_list_goals": "fitfam",
    # Trivia
    "app_trivia_question": "trivia",
    "app_trivia_generate": "trivia",
    "app_trivia_stats": "trivia",
}

#: capability-registry id -> the app that gives it its "app" surface. When the
#: app is gone the capability may still be reachable by voice and chat; it just
#: stops claiming a page that is not there.
CAPABILITY_APPS = {
    "calendar.tasks": "calendar",
    "calendar.timers": "alarmflow",
    "bills.manage": "calendar",
    "health.meds": "calendar",
    "pantry.shopping": "freshtable",
    "food.recipes": "freshtable",
    "food.mealplan": "freshtable",
    "fitness.training": "fitfam",
    "learning.trivia": "trivia",
}


# ---------------------------------------------------------------------------
# requirements
# ---------------------------------------------------------------------------
# `requires` was, for a while, a string nothing read: FreshTable declared
# `network: false` while its page called a model, and Trivia declared nothing
# about the model it cannot work without. A declaration no code consumes is
# decoration, and worse than silence because it reads as a guarantee.
#
# So the box now evaluates it. An unmet requirement NEVER blocks an install —
# an owner may install FreshTable today and add a model tomorrow — it marks the
# app degraded with a reason, and the reason is what the capability says instead
# of failing halfway through an action.

#: requirement key -> (what it needs, the sentence an owner hears).
#: The sentence is completed as "<App>'s <thing> <sentence>".
#: Used when the box KNOWS the requirement is unmet.
REQUIREMENT_REASONS = {
    "ollama": "needs a local model, and none is available on this build",
    "home_assistant": "needs Home Assistant, which this build is not connected to",
    "network": "needs a network connection, and this build has none",
}

#: The same, for when the probe itself could not answer.
#:
#: "None means unknown and is NOT unmet" would avoid a badge that cries wolf,
#: but it is wrong in exactly the case the badge exists for:
#: `config.get_llm_backend()` RAISES on a box with no model, the probe catches
#: it, writes None, and a model-less build would mark nothing degraded at
#: all. Silence is not the safe
#: default when the thing you cannot verify is the thing the app needs; saying
#: "I could not check" is honest, and it is what an owner can act on.
REQUIREMENT_UNVERIFIED = {
    "ollama": "needs a local model, and this build could not verify that one is there",
    "home_assistant": "needs Home Assistant, and this build could not verify it is connected",
    "network": "needs a network connection, and this build could not verify it has one",
}


def declared_requirements(manifest: dict) -> list:
    """The requirement keys this manifest says it needs. Unknown keys ignored.

    An app cannot invent a requirement any more than it can invent a surface:
    a key this build has never heard of is not a veto it gets to cast.
    """
    block = manifest.get("requires")
    if not isinstance(block, dict):
        return []
    return [key for key in REQUIREMENT_REASONS
            if block.get(key) is True]


def unmet_requirements(manifest: dict, available: Optional[dict] = None) -> list:
    """Which of an app's declared requirements this box cannot vouch for.

    ``available`` maps a requirement key to True, False, or None. Only True is
    met. **False is unmet and None is ALSO unmet** — the difference between
    them is the sentence the owner reads, not whether they are told. A probe
    that could not answer is a fact about this build, and hiding it is how a
    model-less box came to report five perfectly healthy apps.

    ``available`` being empty or ``None`` is different again: that is "nobody
    probed anything", the shape every caller who does not care passes, and it
    degrades nothing.
    """
    if not available:
        return []
    return [key for key in declared_requirements(manifest)
            if available.get(key) is not True]


def degraded_reason(manifest: dict, available: Optional[dict] = None) -> str:
    """One owner-readable sentence naming what this app is missing, or ``""``.

    A requirement the box could not check reads "could not verify" rather than
    "none is available": both are degraded, and an owner deserves to know which
    of the two they are looking at.
    """
    unmet = unmet_requirements(manifest, available)
    if not unmet:
        return ""
    name = str(manifest.get("name") or manifest.get("id") or "This app")
    parts = [REQUIREMENT_REASONS[key] if (available or {}).get(key) is False
             else REQUIREMENT_UNVERIFIED[key]
             for key in unmet]
    if len(parts) == 1:
        return f"{name} {parts[0]}."
    return f"{name} " + ", and ".join([", ".join(parts[:-1]), parts[-1]]) + "."


# ---------------------------------------------------------------------------
# fault recording
# ---------------------------------------------------------------------------
# BuildBot routes these through ``puffpai.config_health``. This build has no
# such module, so faults are logged AND kept, because a skipped app that is
# only in a log file is a build quietly missing something.

_problems_lock = threading.Lock()
_problems: list[dict[str, str]] = []


def _record(config_name: str, path: str, detail: str) -> None:
    """Record one manifest fault: logged loudly, and kept for the owner."""
    logger.warning("%s: %s (%s)", config_name, detail, path)
    entry = {"config": config_name, "path": str(path), "detail": str(detail)}
    with _problems_lock:
        _problems.append(entry)


def _clear_problems(config_name: str) -> None:
    """Drop the previous scan's faults for one config, before a rescan.

    Without this a rescan APPENDS, and an owner who fixed a manifest would keep
    being told about the fault they already fixed.
    """
    with _problems_lock:
        _problems[:] = [p for p in _problems if p["config"] != config_name]


def load_problems() -> list[dict[str, str]]:
    """Every manifest fault from the most recent scan, oldest first."""
    with _problems_lock:
        return [dict(p) for p in _problems]


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def _repo_root() -> str:
    """Absolute path of the code tree. Never follows ``$PUFFPAI_DATA_DIR``.

    Manifests are CODE, not runtime state: a hardened unit mounts the code tree
    read-only on purpose, and apps must be read from there. Falls back to the
    path relative to this file when config is not loadable, so a broken install
    still finds its apps.
    """
    try:
        from puffpai.config import get_config
        root = get_config().REPO_ROOT
        if root:
            return root
    except Exception:
        pass
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _apps_dir() -> str:
    """Absolute path of the SHIPPED apps directory, inside the checkout."""
    return os.path.join(_repo_root(), APPS_DIRNAME)


def _packs_dir() -> str:
    """Absolute path of the SHIPPED packs directory, inside the checkout."""
    return os.path.join(_repo_root(), PACKS_DIRNAME)


def user_apps_dir() -> str:
    """Where an owner's OWN apps live: ``<data dir>/apps``.

    A pip-installed or hardened build has no writable checkout - ``apps/`` is
    inside site-packages or under a read-only mount, and an owner who scaffolds a
    persona there is editing the package. So the scan has a second root under the
    writable data directory this build already resolves for every other store
    (``$PUFFPAI_DATA_DIR``, else the per-user state folder; see
    :func:`puffpai.statedir.resolve_runtime_dir`), which is where
    ``puffpai persona new`` writes.

    Shipped folders are scanned FIRST, so an owner folder can never shadow a
    shipped app silently: two folders claiming one id is the existing collision,
    reported by path, and neither is installed.
    """
    return _user_dir(APPS_DIRNAME)


def user_packs_dir() -> str:
    """Where an owner's OWN packs live: ``<data dir>/packs``. See above."""
    return _user_dir(PACKS_DIRNAME)


def _user_dir(name: str) -> str:
    """One of the owner's own roots, resolved lazily and never cached.

    Not cached on purpose: the tests (and a relocated install) move
    ``$PUFFPAI_DATA_DIR`` at runtime, and a cached answer here would leave the
    scan reading a directory nobody is writing to. The resolution is path
    arithmetic, and the scan is already behind :func:`current_manifests`.
    """
    from puffpai import statedir
    return statedir.resolve_runtime_dir(
        os.path.join(_repo_root(), "data", name), env_subdir=name)


def apps_dirs() -> list[str]:
    """Every directory the app scan reads, shipped first."""
    return [_apps_dir(), user_apps_dir()]


def packs_dirs() -> list[str]:
    """Every directory the pack scan reads, shipped first."""
    return [_packs_dir(), user_packs_dir()]


def _within(root: str, candidate: str) -> bool:
    """True when ``candidate`` resolves inside ``root``.

    Symlinks and ``..`` are resolved away before the comparison so neither can
    escape.
    """
    root_real = os.path.realpath(root)
    cand_real = os.path.realpath(candidate)
    return cand_real == root_real or cand_real.startswith(root_real + os.sep)


def resolve_within(root: str, name: str) -> str:
    """Join ``name`` onto ``root`` and refuse if the result escapes.

    Raises ``ValueError`` rather than returning a path the caller might use
    anyway. Every place an app id becomes a path goes through here.
    """
    candidate = os.path.join(root, name)
    if not _within(root, candidate):
        raise ValueError(f"'{name}' resolves outside {root}.")
    return candidate


def app_dir(app_id: str, apps_dir: Optional[str] = None) -> str:
    """The folder an app lives in. Raises ``ValueError`` on an unsafe id."""
    if not valid_id(app_id):
        raise ValueError(f"'{app_id}' is not a valid app id.")
    return resolve_within(apps_dir or _apps_dir(), app_id)


def data_path(app_id: str, data_dir: str) -> str:
    """Where an app's owner data file lives: ``<data_dir>/<id>.json``.

    ``data_dir`` is passed in rather than looked up so :mod:`puffpai.app_data`
    keeps owning the location of the owner's files - the paths existing owners
    already have must not move.
    """
    if not valid_id(app_id):
        raise ValueError(f"'{app_id}' is not a valid app id.")
    if is_issued_id(app_id):
        # A browser app Toolsmith added keeps its data in the bot's copy of its
        # own storage (puffpai.webapps.mirror): no second store for it here.
        raise ValueError(f"'{app_id}' is a browser app; it keeps its own data.")
    return resolve_within(data_dir, f"{app_id}.json")


def is_issued_id(app_id: Any) -> bool:
    """True for an id Toolsmith issued for an app it added (``<slug>-<8>``)."""
    return isinstance(app_id, str) and bool(ISSUED_ID_RE.match(app_id))


def valid_id(app_id: Any) -> bool:
    """True when an app id is safe to use as a path component.

    Either a declarative app's id (lowercase, starts with a letter, letters,
    digits and underscores, 2 to 32 characters) or an id Toolsmith issued
    (:data:`ISSUED_ID_RE`).
    """
    if not isinstance(app_id, str):
        return False
    if is_issued_id(app_id):
        return True
    if not (MIN_ID_LEN <= len(app_id) <= MAX_ID_LEN):
        return False
    return bool(_ID_RE.match(app_id))


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _ascii_text(value: Any, limit: int) -> bool:
    """A printable, bounded, ASCII string. Console-reachable strings only."""
    if not isinstance(value, str) or not value.strip():
        return False
    if len(value) > limit:
        return False
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return all(ch.isprintable() for ch in value)


def validate_manifest(
    data: Any, known_surfaces: Optional[set] = None
) -> Optional[str]:
    """Structural check of one parsed manifest; ``None`` means healthy.

    ``known_surfaces`` is the live surface taxonomy. An app declares membership
    of a surface this build already knows how to gate; it cannot invent one.
    Passing ``None`` reads the taxonomy from :func:`surface_taxonomy`.
    """
    if not isinstance(data, dict):
        return "The manifest is not a JSON object."

    for key in _FORBIDDEN_KEYS:
        if key in data:
            return (
                f"The manifest declares '{key}'. Apps in this version are "
                f"declarative only, so no field may name code to run."
            )

    if str(data.get("schema") or "") != SCHEMA:
        return (
            f"The manifest says schema '{data.get('schema')}'; this build "
            f"reads '{SCHEMA}'."
        )

    if data.get("kind") == WEB_KIND or is_issued_id(data.get("id")):
        return _validate_web_manifest(data)

    app_id = data.get("id")
    if not valid_id(app_id):
        return (
            f"App id {app_id!r} is not usable. An id is {MIN_ID_LEN} to "
            f"{MAX_ID_LEN} characters, lowercase, starting with a letter and "
            f"using only letters, digits and underscores."
        )

    if not _ascii_text(data.get("name"), MAX_LABEL_LEN):
        return f"App '{app_id}' has no usable display name."

    for field in ("blurb", "version", "author", "license"):
        if field in data and not _ascii_text(data.get(field), MAX_LABEL_LEN):
            return f"App '{app_id}' has an unusable '{field}'."

    if FIRST_APP_KEY in data and not isinstance(data.get(FIRST_APP_KEY), bool):
        return f"App '{app_id}' says '{FIRST_APP_KEY}' with something other than true or false."

    if "order" in data:
        order = data.get("order")
        if not isinstance(order, int) or isinstance(order, bool):
            return f"App '{app_id}' has a non-numeric order."
        if not (0 <= order <= MAX_ORDER):
            return f"App '{app_id}' has an order outside 0..{MAX_ORDER}."

    surfaces = data.get("surfaces")
    if not isinstance(surfaces, list) or not surfaces:
        return f"App '{app_id}' names no surfaces."
    if len(surfaces) > MAX_SURFACES:
        return (
            f"App '{app_id}' names {len(surfaces)} surfaces; "
            f"{MAX_SURFACES} is the most an app may claim."
        )
    if known_surfaces is None:
        known_surfaces = set(surface_taxonomy())
    for surface in surfaces:
        if not isinstance(surface, str) or surface not in known_surfaces:
            return (
                f"App '{app_id}' declares surface {surface!r}, which this "
                f"build does not have. An app joins an existing surface; it "
                f"cannot invent one."
            )

    problem = _validate_vocabulary(app_id, data.get("vocabulary"))
    if problem is not None:
        return problem

    problem = _validate_actions(app_id, data.get("actions"))
    if problem is not None:
        return problem

    problem = _validate_data(app_id, data.get("data"))
    if problem is not None:
        return problem

    problem = _validate_privacy(app_id, data)
    if problem is not None:
        return problem

    if "feed" in data:
        # A live-data block: one named outside service, GET only, no code.
        # Validated in full by puffpai.feeds, which refuses unknown fields.
        from puffpai import feeds
        problem = feeds.validate_feed(app_id, data.get("feed"))
        if problem is not None:
            return problem

    return _validate_ui(app_id, data.get("ui"))


def _validate_vocabulary(app_id: Any, vocabulary: Any) -> Optional[str]:
    if vocabulary is None:
        return None
    if not isinstance(vocabulary, dict):
        return f"App '{app_id}' has a vocabulary that is not an object."

    keywords = vocabulary.get("keywords", [])
    if not isinstance(keywords, list):
        return f"App '{app_id}' has a keyword list that is not a list."
    if len(keywords) > MAX_KEYWORDS:
        return (
            f"App '{app_id}' declares {len(keywords)} keywords; "
            f"{MAX_KEYWORDS} is the most one app may add to the router."
        )
    for word in keywords:
        if not _ascii_text(word, MAX_KEYWORD_LEN):
            return f"App '{app_id}' has an unusable keyword: {word!r}."
        if len(word) < MIN_KEYWORD_LEN:
            return (
                f"App '{app_id}' has keyword {word!r}, shorter than "
                f"{MIN_KEYWORD_LEN} characters; it would match nearly "
                f"every message."
            )
        if word != word.lower():
            return f"App '{app_id}' has a keyword that is not lowercase: {word!r}."

    examples = vocabulary.get("examples", [])
    if not isinstance(examples, list):
        return f"App '{app_id}' has an example list that is not a list."
    if len(examples) > MAX_EXAMPLES:
        return (
            f"App '{app_id}' publishes {len(examples)} examples; "
            f"{MAX_EXAMPLES} is the most the phrasebook shows for one app."
        )
    for phrase in examples:
        if not _ascii_text(phrase, MAX_EXAMPLE_LEN):
            return f"App '{app_id}' has an unusable example: {phrase!r}."
        if phrase != phrase.lower():
            return f"App '{app_id}' has an example that is not lowercase: {phrase!r}."
    return None


def _validate_actions(app_id: Any, actions: Any) -> Optional[str]:
    if actions is None:
        return None
    if not isinstance(actions, list):
        return f"App '{app_id}' has an action list that is not a list."
    if len(actions) > MAX_ACTIONS:
        return (
            f"App '{app_id}' declares {len(actions)} actions; "
            f"{MAX_ACTIONS} is the most one app may declare."
        )
    seen: set = set()
    for action in actions:
        if not isinstance(action, dict):
            return f"App '{app_id}' has an action that is not an object."
        for key in _FORBIDDEN_KEYS:
            if key in action:
                return (
                    f"App '{app_id}' has an action declaring '{key}'. An "
                    f"action is a phrase, never a thing to run."
                )
        action_id = action.get("id")
        if not valid_id(action_id):
            return f"App '{app_id}' has an action with an unusable id: {action_id!r}."
        if action_id in seen:
            return f"App '{app_id}' declares action '{action_id}' twice."
        seen.add(str(action_id))
        if not _ascii_text(action.get("says"), MAX_EXAMPLE_LEN):
            return f"App '{app_id}' action '{action_id}' has no usable phrasing."
    return None


def _validate_data(app_id: Any, block: Any) -> Optional[str]:
    if block is None:
        return None
    if not isinstance(block, dict):
        return f"App '{app_id}' has a data block that is not an object."
    default = block.get("default")
    if default is None:
        return None
    if not isinstance(default, dict):
        return f"App '{app_id}' has a default data shape that is not an object."
    try:
        size = len(json.dumps(default).encode("utf-8"))
    except (TypeError, ValueError):
        return f"App '{app_id}' has a default data shape that is not plain JSON."
    if size > MAX_DEFAULT_BYTES:
        return (
            f"App '{app_id}' ships {size} bytes of default data; "
            f"{MAX_DEFAULT_BYTES} is the ceiling. A default is a starting "
            f"shape, not a dataset."
        )
    return None


def _validate_privacy(app_id: Any, data: Any) -> Optional[str]:
    """The five required privacy fields, and nothing else.

    ``doors[].hosts[]``, ``doors[].purpose``, ``doors[].data_sent_plain``,
    ``credential.kind``, ``children.may_be_used_by_minor``. The full 34-field
    contract is deliberately not required here: a ceremony in front
    of an unenforced runtime taxes the honest author and costs a liar ninety
    seconds. ``hosts[]`` is the ONE field this box can independently check, so
    it is the one held to an exact-hostname rule.

    Absent blocks are legitimate on an app that reaches nowhere and holds no
    credential - the five shipped apps are all of that shape - but a block that
    IS present must be complete. Half a declaration is worse than none, because
    the card would render as though the author had answered.
    """
    doors = data.get("doors")
    if doors is not None:
        if not isinstance(doors, list):
            return f"App '{app_id}' has a doors block that is not a list."
        if len(doors) > MAX_DOORS:
            return (
                f"App '{app_id}' declares {len(doors)} doors; {MAX_DOORS} is "
                f"the most one app may name."
            )
        for door in doors:
            if not isinstance(door, dict):
                return f"App '{app_id}' has a door that is not an object."
            for key in _FORBIDDEN_KEYS:
                if key in door:
                    return (
                        f"App '{app_id}' has a door declaring '{key}'. A door "
                        f"is a destination, never a thing to run."
                    )
            hosts = door.get("hosts")
            if not isinstance(hosts, list):
                return (
                    f"App '{app_id}' has a door with no hosts list. Every "
                    f"door names its hosts, even if the list is empty."
                )
            if len(hosts) > MAX_DOOR_HOSTS:
                return (
                    f"App '{app_id}' has a door naming {len(hosts)} hosts; "
                    f"{MAX_DOOR_HOSTS} is the ceiling."
                )
            for host in hosts:
                if not _ascii_text(host, MAX_HOST_LEN):
                    return f"App '{app_id}' has an unusable door host: {host!r}."
                if "*" in host:
                    return (
                        f"App '{app_id}' declares wildcard host {host!r}. A "
                        f"door names exact hostnames; a wildcard cannot be "
                        f"checked and would fail later as a mystery."
                    )
                if _is_ip_literal(host):
                    return (
                        f"App '{app_id}' declares host {host!r}, which is an "
                        f"IP address, not a hostname. A door names a host; "
                        f"this box resolves it and checks where it actually "
                        f"points. An address written directly skips that "
                        f"check, so it is refused."
                    )
                if not _is_registrable_hostname(host):
                    return (
                        f"App '{app_id}' declares host {host!r}, which is not "
                        f"a hostname. Name the host only - no scheme, no port, "
                        f"no path - and give its full name, ending in a real "
                        f"domain suffix."
                    )
            if not _ascii_text(door.get("purpose"), MAX_PURPOSE_LEN):
                return (
                    f"App '{app_id}' has a door with no usable purpose. One "
                    f"line, at most {MAX_PURPOSE_LEN} characters, saying why."
                )
            if not _ascii_text(door.get("data_sent_plain"), MAX_DATA_SENT_LEN):
                return (
                    f"App '{app_id}' has a door that does not say, in the "
                    f"owner's words, what leaves the box."
                )

    credential = data.get("credential")
    if credential is not None:
        if not isinstance(credential, dict):
            return f"App '{app_id}' has a credential block that is not an object."
        kind = credential.get("kind")
        if kind in REFUSED_CREDENTIAL_KINDS:
            return (
                f"App '{app_id}' asks for a '{kind}'. An app never receives "
                f"the owner's password or session cookie."
            )
        if kind not in CREDENTIAL_KINDS:
            return (
                f"App '{app_id}' declares credential kind {kind!r}; this "
                f"build reads {', '.join(CREDENTIAL_KINDS)}."
            )
        # A manifest is a declaration, never a secret store. This refuses the
        # shape of a leaked credential outright rather than trusting the author.
        for key in ("value", "secret", "token", "key", "password"):
            if key in credential:
                return (
                    f"App '{app_id}' puts a '{key}' in its credential block. "
                    f"A manifest declares WHICH secret is needed, never a "
                    f"secret's value."
                )

    children = data.get("children")
    if children is not None:
        if not isinstance(children, dict):
            return f"App '{app_id}' has a children block that is not an object."
        flag = children.get("may_be_used_by_minor")
        if not isinstance(flag, bool):
            return (
                f"App '{app_id}' does not answer 'may_be_used_by_minor' with "
                f"true or false. A parent looks for this field first."
            )
    return None


def _display_text(value: Any, limit: int) -> bool:
    """Printable text for the dashboard (any language), bounded, one line.

    A browser app's name comes from its own title, cleaned by Toolsmith
    (``webapps.install.friendly_name``); it is rendered as plain text only.
    Control, format (for example a right-to-left override), separator and line
    characters other than a plain space are refused.
    """
    import unicodedata
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return False
    return all(ch == " " or unicodedata.category(ch)[0] not in ("C", "Z") for ch in value)


def _validate_web_manifest(data: dict) -> Optional[str]:
    """A browser app's manifest: data only, and only the fields it may have.

    It declares no surfaces, no words for the router, no actions and no data
    block: what the bot can do in the app are Toolsmith-compiled,
    Drawbridge-checked actions kept apart from the manifest, and its data is
    the bot's copy of the app's own storage.
    """
    app_id = data.get("id")
    if data.get("kind") != WEB_KIND or not is_issued_id(app_id):
        return (
            f"App id {app_id!r} is not usable. A browser app's id is the one "
            f"Toolsmith issued, and its manifest says kind 'web'."
        )
    extra = sorted(str(k) for k in data if k not in _WEB_KEYS)
    if extra:
        return f"Browser app '{app_id}' declares fields it may not have: {', '.join(extra[:5])}."
    if not _display_text(data.get("name"), MAX_WEB_NAME_LEN):
        return f"Browser app '{app_id}' has no usable display name."
    if "blurb" in data and not _display_text(data.get("blurb"), MAX_UI_BLURB_LEN):
        return f"Browser app '{app_id}' has an unusable blurb."
    web = data.get("web")
    if not isinstance(web, dict) or set(web) - {"source", "files_root", "entry", "storage"}:
        return f"Browser app '{app_id}' has an unusable web block."
    if web.get("entry") != "index.html" or web.get("storage") != "mirror":
        return f"Browser app '{app_id}' has an unusable web block."
    if not isinstance(web.get("files_root"), str) or len(web["files_root"]) > 1024:
        return f"Browser app '{app_id}' has an unusable web block."
    source = web.get("source", {})
    if not isinstance(source, dict) or set(source) - _WEB_SOURCE_KEYS or not all(
            isinstance(v, str) and len(v) <= 300 for v in source.values()):
        return f"Browser app '{app_id}' has an unusable source."
    outside = data.get("outside", [])
    if not isinstance(outside, list) or len(outside) > MAX_WEB_OUTSIDE:
        return f"Browser app '{app_id}' has an unusable outside list."
    for row in outside:
        if not isinstance(row, dict) or set(row) - {"host", "for", "learned"}:
            return f"Browser app '{app_id}' has an unusable outside list."
        host = row.get("host")
        if not isinstance(host, str) or len(host) > MAX_HOST_LEN or not _HOST_RE.match(host):
            return f"Browser app '{app_id}' names an unusable outside host."
        if not _display_text(row.get("for"), 80) or row.get("learned") != "scan":
            return f"Browser app '{app_id}' has an unusable outside list."
    ui = data.get("ui")
    if not isinstance(ui, dict) or set(ui) != {"route"} \
            or ui.get("route") != f"/dashboard/apps/{app_id}":
        return f"Browser app '{app_id}' must open at /dashboard/apps/{app_id} and nowhere else."
    return None


def _validate_ui(app_id: Any, ui: Any) -> Optional[str]:
    if ui is None:
        return None
    if not isinstance(ui, dict):
        return f"App '{app_id}' has a ui block that is not an object."
    route = ui.get("route")
    if route is not None:
        if not _ascii_text(route, MAX_LABEL_LEN):
            return f"App '{app_id}' has an unusable ui route."
        if not route.startswith("/") or ".." in route:
            return (
                f"App '{app_id}' has ui route {route!r}; a route is an "
                f"absolute in-app path and may not walk upwards."
            )
    for field in ("icon", "widget"):
        if field in ui and not _ascii_text(ui.get(field), MAX_LABEL_LEN):
            return f"App '{app_id}' has an unusable ui {field}."
    return None


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def _sort_key(manifest: dict) -> tuple:
    """Apps are consulted in declared order; id breaks ties.

    Order is what makes the assembled vocabulary reproducible: the shipped five
    carry explicit orders so their words land in the router in the same
    sequence they had when they were one static list.
    """
    order = manifest.get("order", 500)
    if not isinstance(order, int) or isinstance(order, bool):
        order = 500
    return (order, str(manifest.get("id", "")))


def is_first_app(manifest: dict[str, Any]) -> bool:
    """True for a SHIPPED app that starts turned off (``"first_app": true``)."""
    app_id = manifest.get("id") if isinstance(manifest, dict) else None
    if (manifest.get(FIRST_APP_KEY) is not True or not valid_id(app_id)
            or is_issued_id(app_id)):
        return False
    return os.path.isfile(os.path.join(_apps_dir(), str(app_id), MANIFEST_NAME))


def load_manifests(path: Optional[str] = None, *,
                   include_off: bool = False) -> list[dict[str, Any]]:
    """Every valid installed manifest that is turned on, in consultation order.

    Installing is rescanning: this function IS the install mechanism. One bad
    manifest does not take the others down - it is recorded through
    :func:`load_problems` and skipped - and an absent ``apps/`` directory is
    legitimate (a build that carries no apps) and stays quiet.

    Two manifests claiming one id is a collision, and both are dropped with an
    error naming both folders. Keeping the first would make "whichever folder
    sorts earlier" the winner, which is precisely how a hostile app would
    shadow a shipped one; keeping the last is the same bug facing the other
    way. The owner gets told which two folders are fighting and removes one.

    With no ``path`` this scans BOTH roots - the shipped ``apps/`` and the
    owner's :func:`user_apps_dir` - so a build whose checkout is read-only can
    still be extended. A named ``path`` scans exactly that folder and nothing
    else, which is what every caller that passes one means.

    With no ``path``, a first app (:func:`is_first_app`) that this bot has not
    turned on is left out: it is not in the sidebar, not offered to the brain
    and not in the router's words. ``include_off=True`` keeps it, for the few
    callers that must see every app on disk (the data layer, the Apps page's
    "Your first apps" list, the wizard's suggestions, id collision checks).
    """
    _clear_problems(CONFIG_NAME)
    known_surfaces = set(surface_taxonomy())

    by_id: dict = {}
    folder_of: dict = {}
    collided: set = set()

    for target in ([path] if path else apps_dirs()):
        _scan_apps_dir(target, known_surfaces, by_id, folder_of, collided)

    manifests = sorted(by_id.values(), key=_sort_key)
    if len(manifests) > MAX_INSTALLED_APPS:
        _record(
            CONFIG_NAME, path or _apps_dir(),
            f"{len(manifests)} apps are installed; this build carries the "
            f"first {MAX_INSTALLED_APPS}. Remove some and rescan.",
        )
        manifests = manifests[:MAX_INSTALLED_APPS]
    if path is None and not include_off:
        from puffpai import first_apps
        manifests = first_apps.only_on(manifests)
    return manifests


def _scan_apps_dir(target: str, known_surfaces: set, by_id: dict,
                   folder_of: dict, collided: set) -> None:
    """Read ONE apps root into ``by_id`` / ``folder_of``. Never raises.

    Split out of :func:`load_manifests` so the shipped root and the owner's own
    root are read by the same code, in that order. Collisions are reported by
    full path here rather than by folder name: with two roots, "the folders
    'workshop' and 'workshop'" would name nothing an owner could act on.
    """
    try:
        names = sorted(os.listdir(target))
    except OSError as exc:
        # An absent apps/ directory is a build that carries no apps (and an
        # absent owner directory is a build nobody has extended yet), not a
        # fault. Anything else (a permission problem, a file where the folder
        # should be) is worth saying out loud.
        if not isinstance(exc, FileNotFoundError):
            _record(CONFIG_NAME, target, f"The apps directory could not be read: {exc}")
        return

    for name in names:
        if name.startswith(".") or name.startswith("_"):
            continue
        folder = os.path.join(target, name)
        if not os.path.isdir(folder):
            continue
        manifest_file = os.path.join(folder, MANIFEST_NAME)
        if not os.path.isfile(manifest_file):
            _record(
                CONFIG_NAME, folder,
                f"Folder '{name}' is in the apps directory but has no "
                f"{MANIFEST_NAME}, so nothing there is installed.",
            )
            continue
        try:
            size = os.path.getsize(manifest_file)
        except OSError as exc:
            _record(CONFIG_NAME, manifest_file,
                    f"App manifest '{name}' could not be sized: {exc}")
            continue
        if size > MAX_MANIFEST_BYTES:
            _record(
                CONFIG_NAME, manifest_file,
                f"App manifest '{name}' is {size} bytes; the ceiling is "
                f"{MAX_MANIFEST_BYTES}. It was not installed.",
            )
            continue
        try:
            with open(manifest_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            _record(CONFIG_NAME, manifest_file,
                    f"App manifest '{name}' could not be read: {exc}")
            continue

        # Every skip above and below costs ONE app, not the registry: the
        # other apps loaded, and claiming a build-wide fallback would send the
        # owner hunting a fault that is not there.
        problem = validate_manifest(data, known_surfaces)
        if problem is not None:
            _record(CONFIG_NAME, manifest_file,
                    f"App manifest '{name}' was skipped: {problem}")
            continue

        app_id = str(data["id"])
        # The pattern already passed; this is the containment check that has to
        # hold even if the pattern is ever loosened.
        try:
            app_dir(app_id, target)
        except ValueError as exc:
            _record(CONFIG_NAME, manifest_file,
                    f"App manifest '{name}' was skipped: {exc}")
            continue

        if app_id in by_id or app_id in collided:
            first = folder_of.get(app_id, "an earlier folder")
            _record(
                CONFIG_NAME, target,
                f"Two apps claim the id '{app_id}': the folders '{first}' and "
                f"'{folder}'. Neither is installed. Rename or remove one and "
                f"rescan.",
            )
            by_id.pop(app_id, None)
            collided.add(app_id)
            continue

        by_id[app_id] = data
        folder_of[app_id] = folder


# The scan is cheap but not free, and the chat fastpaths ask "is this app
# installed?" on every single turn. Cached behind an explicit rescan so the
# cost is paid once per install rather than once per message, and so "installing
# is rescanning" stays a real, nameable action instead of an accident of
# timing.
#
# CROSS-PROCESS STALENESS. That cache made "an app is live the moment its folder
# is there" true only for the process that installed it. ``puffpai persona new``
# is a DIFFERENT process from the running server: the CLI wrote
# ``<data>/apps/<id>/app.json`` and the server went on serving its cached list,
# so ``GET /apps/installed`` and ``/apps/<id>/data`` answered 404 for a persona
# that existed on disk until somebody called ``POST /apps/rescan`` -- flatly
# contradicting what docs/EXTENDING.md promises. The cache is now
# STALENESS-AWARE: every read compares a cheap fingerprint of the roots and
# rescans when it changed. The fingerprint is not a full scan -- no JSON is
# parsed and no manifest validated -- it is one directory listing per root.
_cache_lock = threading.Lock()
_cached_manifests: Optional[list] = None
_cached_fingerprint: Optional[tuple] = None
#: Bumped on every real (re)scan. Anything holding a derived cache of its own --
#: api_server's AppData instance is the one that matters -- watches this number
#: instead of re-deriving what changed. See :func:`generation`.
_generation = 0
#: Every manifest on disk, turned on or not, keyed by the folders it was read
#: from (a moved state folder reads again). Dropped by :func:`rescan`.
_cached_all: Optional[tuple[tuple[str, ...], list[dict[str, Any]]]] = None


def _root_fingerprint(root: str) -> tuple:
    """A cheap change-detector for ONE app/pack root.

    ``(root, own mtime, ((child name, child mtime), ...))``, from a single
    ``scandir``. Two signals, on purpose, because neither alone is enough on
    every filesystem:

    * the child NAME SET catches the case that matters -- a folder appearing or
      disappearing -- with no reliance on timestamp behaviour at all;
    * the mtimes catch a change one level deeper, i.e. ``app.json`` being
      created inside or removed from an existing folder. On Windows a directory's
      mtime updates when a DIRECT child is added, renamed or removed, which is
      exactly the depth used here and no more. Nothing relies on a directory
      mtime propagating up a tree, because it does not.

    What this does not PROMISE is an in-place EDIT of an existing ``app.json``
    (same name, same folder). It is often caught anyway -- on Windows writing a
    file updates its containing directory's mtime, which is one of the signals
    above -- but whether it is, is a filesystem question, and guaranteeing it
    would cost a stat per app on every chat turn. Editing a manifest is a
    developer action with ``POST /apps/rescan`` sitting right there. Installing,
    uninstalling and scaffolding -- the owner-facing actions, and the ones
    docs/EXTENDING.md makes a promise about -- are all name-set changes.

    A missing root fingerprints as ``(root, None)``; it appearing later is a
    change, which is what makes the FIRST persona on a fresh install live too.
    """
    try:
        with os.scandir(root) as entries:
            children = sorted(
                (entry.name,
                 entry.stat(follow_symlinks=False).st_mtime_ns)
                for entry in entries if not entry.name.startswith("."))
    except OSError:
        return (root, None)
    try:
        own = os.stat(root).st_mtime_ns
    except OSError:
        own = 0
    return (root, own, tuple(children))


def _roots_fingerprint() -> tuple:
    """The change-detector for every root the registry reads, apps and packs.

    Packs are included because :func:`load_packs` is resolved against the app
    manifests this cache holds, and a pack root that changed without the app
    root changing is still a changed build.

    Never raises: a fingerprint that cannot be taken answers ``()``, which
    compares unequal to any real one and therefore errs towards rescanning
    rather than towards serving a stale list.
    """
    try:
        return tuple(_root_fingerprint(root)
                     for root in (apps_dirs() + packs_dirs()))
    except Exception:
        return ()


def generation() -> int:
    """How many times the registry has really (re)scanned.

    The invalidation signal for caches DERIVED from the manifests. A holder
    compares this to the value it last built at; equal means its tables are
    still the ones these manifests imply.
    """
    return _generation


def register_egress_doors(manifests: Optional[list] = None) -> tuple:
    """Put every installed app's declared doors in the enforced door table.

    Called on each (re)scan, which is what install and uninstall ARE here — so
    an app whose folder is gone loses its doors in the same breath, and the
    owner's old "on" for it stops resolving against anything.

    Every registered door is forced shut (``default=False`` is a literal in
    ``egress_doors``, never a manifest key), namespaced ``app:<id>:<n>`` so a
    manifest cannot shadow a built-in door, and carries its declared hosts as
    an exact allowlist. The owner opens one with the existing
    ``POST /system/egress/door``; nothing an app or its author can write opens
    anything.

    Never raises. If the egress module will not import, the doors are simply
    not enforced and :func:`hosts_enforcement` says so for every app rather
    than the surfaces claiming a control that is not there.
    """
    entries: list = []
    declared: dict = {}
    for manifest in (manifests if manifests is not None else []):
        if not isinstance(manifest, dict):
            continue
        app_id = manifest.get("id")
        if not valid_id(app_id):
            continue
        doors = manifest.get("doors")
        doors = doors if isinstance(doors, list) else []
        entries.append((str(app_id), doors))
        declared[str(app_id)] = len(doors)

    enforced: set = set()
    registered: tuple = ()
    try:
        from puffpai.security import egress_doors
        registered = egress_doors.register_app_doors(entries)
        got: dict = {}
        for door_id in registered:
            owner = egress_doors.app_of(door_id)
            if owner:
                got[owner] = got.get(owner, 0) + 1
        for app_id, count in declared.items():
            # An app is "enforced" only when EVERY door it declared reached the
            # table. A partial registration is a claim the box cannot back, and
            # the honest label for it is the one that shipped before.
            if got.get(app_id, 0) == count:
                enforced.add(app_id)
    except Exception as exc:
        # Egress policy must never gate the app registry: an appliance that
        # cannot load its apps because a security module is unhappy is a worse
        # outcome than one whose apps are labelled declared_only. The label
        # below is what keeps that honest.
        _record(CONFIG_NAME, "egress_doors",
                f"Manifest doors could not be registered with the egress door "
                f"table, so this build does not enforce them: {exc}")
        enforced = set()

    global _enforced_apps, _declared_apps
    _enforced_apps = enforced
    _declared_apps = set(declared)
    return registered


def hosts_enforcement(app_id: Any = None) -> tuple:
    """``(label, notice)`` for one app — what the box REALLY does with its hosts.

    ``"enforced"`` only when this app's declared doors are in the enforced door
    table right now. Anything else keeps the ``"declared_only"`` line it had
    before the keystone, because a surface that renders the stronger sentence
    over an unregistered door is the exact defect the honest label existed to
    prevent.

    ``app_id=None`` asks the aggregate question — "does this box enforce app
    hosts" — and answers it the only way an aggregate honestly can: the WEAKEST
    member decides. One app whose doors did not register makes the whole answer
    ``declared_only``, because a top-level banner reading "enforced" over a list
    containing that app is the overclaim in a different place.

    Before any scan has run, ``_enforced_apps`` is empty and the aggregate is
    ``declared_only`` — not "enforced" by vacuous truth. A caller that asks
    early must not be told the box enforces something it has not yet looked at,
    and it must not disagree with the per-app answers, which are all
    ``declared_only`` in that state.
    """
    if app_id is None:
        if not _enforced_apps or not _declared_apps <= _enforced_apps:
            return HOSTS_DECLARED_ONLY, HOSTS_DECLARED_ONLY_NOTICE
        return HOSTS_ENFORCEMENT, HOSTS_ENFORCEMENT_NOTICE
    if str(app_id) in _enforced_apps:
        return HOSTS_ENFORCEMENT, HOSTS_ENFORCEMENT_NOTICE
    return HOSTS_DECLARED_ONLY, HOSTS_DECLARED_ONLY_NOTICE


def current_manifests() -> list:
    """The installed manifests, rescanning only when the roots have changed.

    Reused between reads, as before -- but no longer until an explicit
    :func:`rescan`: the cheap fingerprint above decides. That is what makes an
    app installed by ANOTHER process (``puffpai persona new``) live on the next
    read of the running server, without a scan per chat turn.
    """
    global _cached_manifests, _cached_fingerprint, _generation
    scanned = None
    fingerprint = _roots_fingerprint()
    with _cache_lock:
        if _cached_manifests is None or fingerprint != _cached_fingerprint:
            _cached_manifests = load_manifests()
            # Taken AFTER the scan, not reused from above: a folder that
            # appeared while the scan was running is then still pending, and the
            # next read picks it up instead of the change being swallowed.
            _cached_fingerprint = _roots_fingerprint()
            _generation += 1
            scanned = list(_cached_manifests)
        result = list(_cached_manifests)
    if scanned is not None:
        # Outside the lock on purpose: the egress registry takes a lock of its
        # own, and holding two of them across a module boundary is how a
        # deadlock gets built. Nothing here reads _cached_manifests.
        register_egress_doors(scanned)
    return result


def all_manifests() -> list[dict[str, Any]]:
    """Every manifest on disk, first apps that are off included, scanning
    once per set of folders and reusing until :func:`rescan`."""
    global _cached_all
    key = tuple(apps_dirs())
    with _cache_lock:
        if _cached_all is None or _cached_all[0] != key:
            _cached_all = (key, load_manifests(include_off=True))
        return list(_cached_all[1])


def rescan() -> list:
    """Re-read ``apps/`` from disk. This IS the install/uninstall mechanism.

    Still explicit, and still the thing install and uninstall CALL -- the
    staleness check makes it unnecessary for the common case, not wrong. It
    remains the only way to pick up an in-place manifest edit (see
    :func:`_root_fingerprint`), and the only way to force a rescan when a
    filesystem's timestamps cannot be trusted at all.
    """
    global _cached_manifests, _cached_fingerprint, _cached_all
    with _cache_lock:
        _cached_manifests = None
        _cached_fingerprint = None
        _cached_all = None
    manifests = current_manifests()
    # A rescan IS install and uninstall, so it is also when an app's outside
    # service (its feed host) is registered or dropped. Every one starts shut.
    try:
        from puffpai import feeds
        feeds.sync_doors(manifests)
    except Exception as exc:  # pragma: no cover - never blocks a rescan
        _record(CONFIG_NAME, "feeds",
                f"Live-data services could not be registered: {exc}")
    return manifests


def installed_ids(manifests: Optional[list] = None) -> list:
    """The ids of the installed apps, in consultation order."""
    return [str(m["id"]) for m in (manifests if manifests is not None
                                   else current_manifests())]


def default_data(manifest: dict) -> dict:
    """A fresh copy of one app's declared starting shape.

    A copy every time: the manifest is loaded once and the caller mutates what
    it gets back, so handing out the manifest's own dict would let one app's
    first write become every later owner's "default".
    """
    block = manifest.get("data")
    default = block.get("default") if isinstance(block, dict) else None
    if not isinstance(default, dict):
        return {}
    return json.loads(json.dumps(default))


# ---------------------------------------------------------------------------
# packs
# ---------------------------------------------------------------------------

def validate_pack(data: Any, installed: Optional[set] = None) -> Optional[str]:
    """Structural check of one parsed pack manifest; ``None`` means healthy.

    A pack is a LIST, not a container: it names apps that must already be
    installed, and it carries no data, no vocabulary, no ui and no code of its
    own. ``installed`` is the set of installed app ids; a pack naming an app
    this build does not have is refused with the missing ids named, because a
    pack that silently drops half its apps is a persona that quietly is not the
    persona the owner chose.
    """
    if not isinstance(data, dict):
        return "The pack manifest is not a JSON object."

    for key in _FORBIDDEN_KEYS:
        if key in data:
            return (
                f"The pack declares '{key}'. A pack is a list of apps, so no "
                f"field may name code to run."
            )

    if str(data.get("schema") or "") != PACK_SCHEMA:
        return (
            f"The pack says schema '{data.get('schema')}'; this build reads "
            f"'{PACK_SCHEMA}'."
        )

    pack_id = data.get("id")
    if not valid_id(pack_id):
        return f"Pack id {pack_id!r} is not usable."

    if not _ascii_text(data.get("display_name"), MAX_LABEL_LEN):
        return f"Pack '{pack_id}' has no usable display name."

    for field in ("blurb", "version", "author", "license", "posture_min"):
        if field in data and not _ascii_text(data.get(field), MAX_LABEL_LEN):
            return f"Pack '{pack_id}' has an unusable '{field}'."

    apps = data.get("apps")
    if not isinstance(apps, list) or not apps:
        return f"Pack '{pack_id}' lists no apps."
    if len(apps) > MAX_PACK_APPS:
        return (
            f"Pack '{pack_id}' lists {len(apps)} apps; {MAX_PACK_APPS} is the "
            f"most one pack may carry."
        )
    seen: set = set()
    missing: list = []
    for entry in apps:
        if not isinstance(entry, dict):
            return f"Pack '{pack_id}' has an app entry that is not an object."
        entry_id = entry.get("id")
        if not valid_id(entry_id):
            return f"Pack '{pack_id}' lists an unusable app id: {entry_id!r}."
        if entry_id in seen:
            return f"Pack '{pack_id}' lists app '{entry_id}' twice."
        seen.add(str(entry_id))
        version = entry.get("version")
        if version is not None and not _ascii_text(version, MAX_LABEL_LEN):
            return f"Pack '{pack_id}' has an unusable version for '{entry_id}'."
        if installed is not None and entry_id not in installed:
            missing.append(str(entry_id))
    if missing:
        return (
            f"Pack '{pack_id}' lists {', '.join(sorted(missing))}, which "
            f"{'is' if len(missing) == 1 else 'are'} not installed on this "
            f"build. Install the missing app"
            f"{'' if len(missing) == 1 else 's'} and rescan."
        )
    return None


def _off_first_apps(have: dict) -> list[dict[str, Any]]:
    """The first apps on disk that are not in *have* (turned off). Never raises."""
    try:
        return [m for m in load_manifests(include_off=True)
                if is_first_app(m) and str(m.get("id")) not in have]
    except Exception:  # noqa: BLE001
        return []


def load_packs(path: Optional[str] = None,
               manifests: Optional[list] = None,
               available: Optional[dict] = None) -> list:
    """Every valid installed pack, sorted by id, with its apps resolved.

    Packs load AFTER apps because a pack is validated against what is actually
    installed. Each returned pack carries a ``privacy`` summary DERIVED from its
    apps' own declarations - never written by the pack author, who is not
    necessarily the author of the apps and must not get to speak for them.

    With no ``path`` both roots are read - the shipped ``packs/`` and the owner's
    :func:`user_packs_dir` - and one id per BUILD still holds across the two.
    """
    _clear_problems(PACK_CONFIG_NAME)
    app_manifests = manifests if manifests is not None else load_manifests()

    if path is None:
        merged: dict = {}
        for root in packs_dirs():
            for record in _load_packs_from(root, app_manifests, available):
                pack_id = str(record["id"])
                if pack_id in merged:
                    _record(PACK_CONFIG_NAME, root,
                            f"Two packs claim the id '{pack_id}', in different "
                            f"folders. Neither is installed. Rename or remove "
                            f"one and rescan.")
                    merged.pop(pack_id, None)
                    continue
                merged[pack_id] = record
        return [merged[key] for key in sorted(merged)][:MAX_INSTALLED_PACKS]

    return _load_packs_from(path, app_manifests, available)


def _load_packs_from(target: str, app_manifests: list,
                     available: Optional[dict]) -> list:
    """Read ONE packs root. Records its own faults; never clears them."""
    by_app_id = {str(m["id"]): m for m in app_manifests if valid_id(m.get("id"))}
    # A first app that is turned off is still on disk: a pack naming it is not
    # broken (puffpai.first_apps).
    for first in _off_first_apps(by_app_id):
        by_app_id.setdefault(str(first["id"]), first)

    try:
        names = sorted(os.listdir(target))
    except OSError as exc:
        if not isinstance(exc, FileNotFoundError):
            _record(PACK_CONFIG_NAME, target,
                    f"The packs directory could not be read: {exc}")
        return []

    out: list = []
    seen: set = set()
    for name in names:
        if name.startswith(".") or name.startswith("_"):
            continue
        folder = os.path.join(target, name)
        if not os.path.isdir(folder):
            continue
        manifest_file = os.path.join(folder, PACK_MANIFEST_NAME)
        if not os.path.isfile(manifest_file):
            _record(PACK_CONFIG_NAME, folder,
                    f"Folder '{name}' is in the packs directory but has no "
                    f"{PACK_MANIFEST_NAME}, so nothing there is installed.")
            continue
        try:
            if os.path.getsize(manifest_file) > MAX_MANIFEST_BYTES:
                _record(PACK_CONFIG_NAME, manifest_file,
                        f"Pack manifest '{name}' is over the "
                        f"{MAX_MANIFEST_BYTES}-byte ceiling; it was not installed.")
                continue
            with open(manifest_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            _record(PACK_CONFIG_NAME, manifest_file,
                    f"Pack manifest '{name}' could not be read: {exc}")
            continue

        problem = validate_pack(data, set(by_app_id))
        if problem is not None:
            _record(PACK_CONFIG_NAME, manifest_file,
                    f"Pack manifest '{name}' was skipped: {problem}")
            continue

        pack_id = str(data["id"])
        if pack_id in seen:
            _record(PACK_CONFIG_NAME, target,
                    f"Two packs claim the id '{pack_id}'. Neither is "
                    f"installed. Rename or remove one and rescan.")
            out = [p for p in out if p["id"] != pack_id]
            continue
        seen.add(pack_id)
        out.append(pack_record(data, by_app_id, available))

        if len(out) > MAX_INSTALLED_PACKS:
            _record(PACK_CONFIG_NAME, target,
                    f"More than {MAX_INSTALLED_PACKS} packs are installed; "
                    f"the rest were not loaded.")
            out = out[:MAX_INSTALLED_PACKS]
            break
    return out


def pack_privacy_summary(app_ids: list, by_app_id: dict) -> dict:
    """The pack's privacy line, aggregated from the apps it lists.

    Derived, never declared: the pack author does not get to summarise somebody
    else's app. ``hosts`` is the union of every door's hosts, ``credentials``
    the set of credential kinds other than "none", and
    ``child_safe_declared`` is true only when EVERY listed app says so - one app
    that will not answer for a minor makes the pack unsuitable for one.

    "Declared" is load-bearing in that name. Every field here is an app author's
    own claim; the box has verified none of it, and a surface that renders this
    as "child safe" is lying on our behalf.
    """
    hosts: list = []
    credentials: list = []
    child_flags: list = []
    for app_id in app_ids:
        manifest = by_app_id.get(app_id)
        if not isinstance(manifest, dict):
            continue
        for door in (manifest.get("doors") or []):
            if not isinstance(door, dict):
                continue
            for host in (door.get("hosts") or []):
                if isinstance(host, str) and host not in hosts:
                    hosts.append(host)
        credential = manifest.get("credential")
        if isinstance(credential, dict):
            kind = credential.get("kind")
            if isinstance(kind, str) and kind != "none" and kind not in credentials:
                credentials.append(kind)
        children = manifest.get("children")
        if isinstance(children, dict):
            child_flags.append(bool(children.get("may_be_used_by_minor")))
    # The weakest member decides. A pack whose apps are half enforced must not
    # render the stronger sentence over the half that is not.
    label, notice = HOSTS_ENFORCEMENT, HOSTS_ENFORCEMENT_NOTICE
    for app_id in app_ids:
        one_label, one_notice = hosts_enforcement(app_id)
        if one_label != HOSTS_ENFORCEMENT:
            label, notice = one_label, one_notice
            break
    else:
        if not app_ids:
            label, notice = hosts_enforcement(None)
    return {
        "hosts": sorted(hosts),
        "hosts_enforcement": label,
        "hosts_notice": notice,
        "credentials": sorted(credentials),
        # DECLARED, never "safe". This is an AND-fold over five boxes the app
        # authors ticked themselves; nothing here has been verified by anything.
        # The name is the honesty: a parent reading "child_safe" would hear a
        # judgement the box has not made.
        "child_safe_declared": bool(child_flags) and all(child_flags),
    }


def pack_record(data: dict, by_app_id: dict,
                available: Optional[dict] = None) -> dict:
    """One validated pack as the API hands it out. Fields copied by name."""
    app_ids = [str(e.get("id")) for e in (data.get("apps") or [])
               if isinstance(e, dict) and valid_id(e.get("id"))]
    return {
        "id": str(data["id"]),
        "display_name": str(data.get("display_name") or data["id"])[:MAX_UI_NAME_LEN],
        "blurb": str(data.get("blurb") or "")[:MAX_UI_BLURB_LEN],
        "version": str(data.get("version") or "")[:MAX_UI_NAME_LEN],
        "posture_min": str(data.get("posture_min") or "")[:MAX_UI_NAME_LEN],
        "apps": [
            {"id": str(e.get("id")),
             "version": str(e.get("version") or "")[:MAX_UI_NAME_LEN]}
            for e in (data.get("apps") or [])
            if isinstance(e, dict) and valid_id(e.get("id"))
        ],
        "privacy": pack_privacy_summary(app_ids, by_app_id),
        # A pack is only as installed as its least-ready app.
        "degraded_apps": [
            {"id": app_id,
             "reason": degraded_reason(by_app_id[app_id], available)}
            for app_id in app_ids
            if app_id in by_app_id
            and degraded_reason(by_app_id[app_id], available)
        ],
    }


# ---------------------------------------------------------------------------
# router vocabulary
# ---------------------------------------------------------------------------

def assemble_vocabulary(manifests: Optional[list] = None) -> dict:
    """The words and published phrasings of the installed apps, concatenated.

    This is the owner's requirement 5 made mechanical: the router's app
    vocabulary is the apps the build actually has. Remove an app's folder and
    its words leave with it; drop a stranger's folder in and its words arrive,
    without a line of core changing.

    Order follows :func:`_sort_key`, and duplicates collapse onto their first
    appearance so two apps both claiming "reminder" cost one scan, not two, and
    the first app declared keeps its position.
    """
    keywords: list = []
    examples: list = []
    for manifest in (manifests if manifests is not None else load_manifests()):
        vocabulary = manifest.get("vocabulary")
        if not isinstance(vocabulary, dict):
            continue
        for word in (vocabulary.get("keywords") or []):
            if isinstance(word, str) and word not in keywords:
                keywords.append(word)
        for phrase in (vocabulary.get("examples") or []):
            if isinstance(phrase, str) and phrase not in examples:
                examples.append(phrase)
    return {"keywords": keywords, "examples": examples}


# ---------------------------------------------------------------------------
# what the dashboard is told
# ---------------------------------------------------------------------------

def _safe_ui_route(route: Any) -> str:
    """An app's route, or ``""`` when it is not something a browser may follow.

    An app with no usable route still exists - it keeps its words, its data and
    its capability record. It just gets no nav entry, because there is nowhere
    honest to send the owner.
    """
    if not isinstance(route, str) or not route.startswith("/"):
        return ""
    if route.startswith("//") or ".." in route:
        return ""
    if not _SAFE_ROUTE_RE.match(route) or len(route) > MAX_LABEL_LEN:
        return ""
    return route


def ui_records(manifests: Optional[list] = None,
               available: Optional[dict] = None) -> list:
    """The installed apps as the dashboard needs them, field by field.

    Every field is copied out by name and bounded here. A manifest is never
    echoed wholesale: an app author is not us, and a field this build has no
    opinion about does not get to reach a browser just because it was in the
    file. ``name`` and ``blurb`` are the app author's words and are rendered as
    text - escaped like any other string, never markup, never a prompt.
    ``icon`` is a NAME, not an SVG path; the dashboard looks it up in its own
    icon set and falls back when it does not recognise it, so the worst a
    hostile icon field can do is be ignored.
    """
    out: list = []
    for manifest in (manifests if manifests is not None else load_manifests()):
        app_id = manifest.get("id")
        if not valid_id(app_id):
            continue
        raw_ui = manifest.get("ui")
        ui = raw_ui if isinstance(raw_ui, dict) else {}
        surfaces = [
            str(s) for s in (manifest.get("surfaces") or [])
            if isinstance(s, str)
        ][:MAX_SURFACES]
        order = manifest.get("order", 500)
        if not isinstance(order, int) or isinstance(order, bool):
            order = 500
        icon = ui.get("icon")
        reason = degraded_reason(manifest, available)
        hosts_label, hosts_notice = hosts_enforcement(app_id)
        out.append({
            "id": str(app_id),
            "degraded": bool(reason),
            "degraded_reason": reason,
            "requires": declared_requirements(manifest),
            "hosts_enforcement": hosts_label,
            "hosts_notice": hosts_notice,
            "name": str(manifest.get("name") or app_id)[:MAX_UI_NAME_LEN],
            "blurb": str(manifest.get("blurb") or "")[:MAX_UI_BLURB_LEN],
            "route": _safe_ui_route(ui.get("route")),
            "icon": (str(icon)[:MAX_UI_ICON_LEN] if isinstance(icon, str) else ""),
            "surfaces": surfaces,
            "order": max(0, min(int(order), MAX_ORDER)),
            "feed": _feed_summary(manifest),
        })
    return out


def _feed_summary(manifest: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The app's live-data card, described in bounded fields, or None."""
    if "feed" not in manifest:
        return None
    try:
        from puffpai import feeds
        return feeds.ui_summary(manifest)
    except Exception:  # pragma: no cover - a record never fails on this
        return None


__all__ = [
    "SCHEMA",
    "PACK_SCHEMA",
    "APPS_SURFACE",
    "APPS_DIRNAME",
    "PACKS_DIRNAME",
    "MANIFEST_NAME",
    "PACK_MANIFEST_NAME",
    "MAX_MANIFEST_BYTES",
    "MAX_INSTALLED_APPS",
    "MAX_KEYWORDS",
    "MAX_EXAMPLES",
    "MAX_ACTIONS",
    "CORE_TOOL_APPS",
    "CAPABILITY_APPS",
    "all_manifests",
    "app_dir",
    "apps_dirs",
    "packs_dirs",
    "user_apps_dir",
    "user_packs_dir",
    "assemble_vocabulary",
    "data_path",
    "default_data",
    "installed_ids",
    "current_manifests",
    "generation",
    "load_manifests",
    "load_packs",
    "rescan",
    "load_problems",
    "declared_requirements",
    "degraded_reason",
    "unmet_requirements",
    "HOSTS_ENFORCEMENT",
    "HOSTS_ENFORCEMENT_NOTICE",
    "HOSTS_DECLARED_ONLY",
    "HOSTS_DECLARED_ONLY_NOTICE",
    "hosts_enforcement",
    "register_egress_doors",
    "REQUIREMENT_REASONS",
    "REQUIREMENT_UNVERIFIED",
    "pack_privacy_summary",
    "pack_record",
    "is_first_app",
    "FIRST_APP_KEY",
    "resolve_within",
    "surface_taxonomy",
    "ui_records",
    "valid_id",
    "is_issued_id",
    "ISSUED_ID_RE",
    "WEB_KIND",
    "validate_manifest",
    "validate_pack",
]
