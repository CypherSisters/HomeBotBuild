"""Zone 3, the Bridge - what this bot is allowed to reach, decided on purpose.

Step 6 of the wizard. The Bridge is a **policy** choice: four postures, plainly
described, one of which the owner picks. An owner with no firewall and no
managed network still walks this step - in fact they are the person it exists
for, because the bot's own fence is the only fence they have. The step is never
skipped on the grounds that there is nothing to configure.

Scope, stated so it stays true
------------------------------
This module is policy and nothing else. It does not scan a network, enumerate
hosts, resolve names, or classify devices, and it must never grow the ability
to. The device-classification cascade is withheld from the open base; the
posture step deliberately does not depend on it, because what a bot is *allowed*
to reach is a decision the owner makes, not a discovery the software performs.
The only inputs here are three booleans the owner answered out loud.

The step also asks what is at the edge of the network already - nothing, this
build's own Zone 3, or gear the owner already runs. That answer is **data**: a
recorded fact about the household. Picking Zone 3 does not start Zone 3, and
naming your own Pi-hole does not make anything talk to it. See
:func:`edge_manifest_block`.

Two rules the reconciliation obeys
----------------------------------
* **The stricter of the two choices wins.** ``run_egress`` from Step 0 sets a
  floor; a posture below that floor is raised to it. Nothing in here can lower a
  posture the owner chose.
* **The ordering lives in code.** ``config/bridge_postures.json`` carries the
  wording. The strictness ranking that the math uses is a constant below, so a
  hand-edited config file can change how a posture reads but never how strict it
  is.

What is enforced
----------------
The choice is read back at run time by
:func:`puffpai.security.bridge_guard.reach_level`, and three places
act on it. Nothing here scans or resolves anything; the acting is done by the
code that already sits at each exit:

* ``open_yard`` and ``fenced`` (level ``open``): the home network is allowed,
  and every outside service still waits for its own switch.
* ``moat`` (level ``switched_on``): only the outside services the owner
  switched on reach the internet. The home network still works (Home
  Assistant, the media server, phones, other devices at home). A setting in
  a file or the environment cannot switch an outside service on, and
  :mod:`puffpai.security.bridge_guard` refuses, at the moment a connection
  is made, any internet connection that did not come through a listed exit
  (each of which checks the owner's switch).
* ``drawbridge_up`` (level ``sealed``): nothing reaches the internet,
  switched on or not. The home network still works.
* A build whose ``run_egress`` is ``sealed`` (level ``sealed`` and
  ``home_network`` False): nothing leaves this computer at all, the home
  network included, which is what that build setting promises.

A record that cannot be read, or a Bridge value nobody knows, is read as
``drawbridge_up``: when the choice is unclear, the stricter rule applies.

All strings are ASCII: they reach a Windows console.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

# Strictness ordering. Policy, not copy - deliberately not read from the JSON,
# so editing the shipped file cannot loosen anything.
_STRICTNESS: dict[str, int] = {
    "open_yard": 0,
    "fenced": 1,
    "moat": 2,
    "drawbridge_up": 3,
}

# The setting used only when no Bridge was chosen (before setup, or a build
# whose record names none). It does not open the internet: every outside
# service ships off and is used only after the owner switches it on. A build
# that pins its own Bridge records it (the HomeBot preset records Moat).
DEFAULT_POSTURE = "fenced"

# Postures that are no longer offered, and the one each now reads as. Every
# posture is still offered here, so nothing is folded; the map stays so a
# record from a build that retires one is read the same way everywhere.
RETIRED_POSTURES: dict[str, str] = {}

# Shown next to a record that was folded by RETIRED_POSTURES.
MOAT_FOLDED_NOTE = (
    "The setting this record named is no longer offered, so the closest one "
    "that is still offered is used instead. You can pick another at any time."
)

# Never handed out as a recommendation. Open Yard is a legitimate choice, but it
# is the one posture that has to be chosen deliberately rather than accepted
# because the wizard suggested it.
_NEVER_RECOMMENDED = frozenset({"open_yard"})

# run_egress (Step 0) sets the floor a posture cannot sit below. An unrecognised
# value falls back to the safe default rather than to the loosest option.
_EGRESS_FLOOR: dict[str, str] = {
    "sealed": "drawbridge_up",
    "island": "fenced",
    "enumerated": "open_yard",
}
_UNKNOWN_EGRESS_FLOOR = DEFAULT_POSTURE

_CHOSEN_BY = frozenset({"owner", "default", "override"})

# Baked-in fallback so the Bridge step still runs if the shipped file is missing
# or unreadable. Same four postures, same ids, shortened wording; the full copy
# lives in config/bridge_postures.json and is what maintainers edit.
_FALLBACK_POSTURES: dict[str, Any] = {
    "schema": "bridge_postures/v1",
    "default": DEFAULT_POSTURE,
    "postures": [
        {
            "id": "open_yard",
            "label": "Open Yard",
            "strictness": 0,
            "recommendable": False,
            "summary": "Everything you already said yes to happens without asking you again.",
            "allows": {
                "lan_devices": "Reaches any device you listed, with no prompt each time.",
                "actions": "Actions run without a per-use prompt.",
                "egress": "Outside services you switched on stay on while the bot runs.",
                "consent": "Asked once at setup, not again at each use.",
            },
            "costs": "Nothing to click, and nothing to catch a mistake either.",
            "matches_run_egress": "enumerated",
        },
        {
            "id": "fenced",
            "label": "Fenced",
            "strictness": 1,
            "recommendable": True,
            "summary": "Your bot can talk to things in your house, and anything past it is gated.",
            "allows": {
                "lan_devices": "Devices on your own network are allowed.",
                "actions": "Actions are gated at the boundary before they run.",
                "egress": "Outside services are listed one by one, and every one starts off.",
                "consent": "Asked when an action reaches past what you already allowed.",
            },
            "costs": "You will confirm things now and then.",
            "matches_run_egress": "island",
        },
        {
            "id": "moat",
            "label": "Moat",
            "strictness": 2,
            "recommendable": True,
            "summary": ("Only the outside services you switched on reach the "
                        "internet. Your home network still works."),
            "allows": {
                "lan_devices": "Your home network works as usual.",
                "actions": "Nothing reaches the internet unless you switched that service on.",
                "egress": "Only the outside services you switched on, each one by you.",
                "consent": "You switch each outside service on yourself, one at a time.",
            },
            "costs": "A new outside service does nothing until you switch it on.",
            "matches_run_egress": "island",
        },
        {
            "id": "drawbridge_up",
            "label": "Drawbridge Up",
            "strictness": 3,
            "recommendable": True,
            "summary": ("Nothing reaches the internet. Your home network still "
                        "works."),
            "allows": {
                "lan_devices": "Your home network works as usual.",
                "actions": "Anything that needs the internet does not run, and the bot says so.",
                "egress": "None. Every outside service is off and cannot be switched on.",
                "consent": "Nothing to switch on, because nothing reaches the internet.",
            },
            "costs": "Anything that needs the internet is off.",
            "matches_run_egress": "island",
        },
    ],
}


# ---------------------------------------------------------------------------
# the network edge: what is in front of this box, if anything
#
# Three answers, and "nothing" is one of them. A household with no edge device
# is not a household with a half-configured Bridge: the posture ladder above is
# the fence, and it is the WHOLE fence for exactly that person. Nothing in this
# section may make the absence of gear read as a missing setting - see
# ``declared`` on the block below, which says out loud that the owner answered.
#
# This is still policy and copy only. Whether Zone 3 could run here is a fact
# about the machine, and facts about the machine are discovered in
# :mod:`puffpai.setup_ops`; the support verdict arrives here as an argument.
# ---------------------------------------------------------------------------

#: The three answers to "what protects your network edge today?".
EDGE_KINDS: tuple[str, ...] = ("none", "zone3", "external")

DEFAULT_EDGE_KIND = "none"

#: What each answer means, in the owner's words.
_EDGE_COPY: dict[str, dict[str, str]] = {
    "none": {
        "label": "Nothing in front of it",
        "description": (
            "No Pi-hole, no filtering router, no appliance. Your bot's own "
            "posture above is the fence, and it is a real one."
        ),
        "summary": (
            "You said there is nothing at the edge of your network. The Bridge "
            "posture you picked is the whole fence, and it is configured."
        ),
    },
    "zone3": {
        "label": "HomeBot's own network edge (Zone 3)",
        "description": (
            "A local DNS sinkhole HomeBot manages, in Docker, on this box. "
            "Linux and Docker only. It ships inert: nothing points at it until "
            "you change one field on your router yourself."
        ),
        "summary": (
            "You asked for HomeBot's own network edge. The wizard changed "
            "nothing on your network; the commands to bring it up are listed "
            "for you to run when you mean it."
        ),
    },
    "external": {
        "label": "My own (Pi-hole, AdGuard, a firewall, a home lab)",
        "description": (
            "You already run something at the edge. HomeBot records what it is "
            "so the build record is true, and touches it in no other way."
        ),
        "summary": (
            "You already run your own edge. It is recorded as a fact about "
            "this household; HomeBot never contacts it."
        ),
    },
}

#: The kinds of edge gear an owner may already have. Free text is fine too -
#: this list is a menu, not a whitelist, and an unlisted answer is recorded
#: verbatim rather than rounded to "other".
EXTERNAL_EDGE_KINDS: tuple[tuple[str, str], ...] = (
    ("pihole", "Pi-hole"),
    ("adguard", "AdGuard Home"),
    ("firewall", "A firewall appliance (pfSense, OPNsense, UniFi, ...)"),
    ("homelab", "A home lab, or something I built myself"),
    ("other", "Something else"),
)

#: The section of ``docs/ZONE3_SETUP.md`` that governs bringing Zone 3 up
#: WITHOUT touching the network. Cited rather than paraphrased, so the wizard
#: and the document cannot drift. Each entry is a heading as written in the doc;
#: ``A - B`` cites subsection ``B`` of section ``A``, and BOTH halves have to
#: exist verbatim (``tests/test_zone3_doc_sections.py`` is the standing check -
#: this list said "Start Pi-hole" for a step the doc renamed to AdGuard Home).
ZONE3_DOC = "docs/ZONE3_SETUP.md"
ZONE3_DOC_SECTIONS: tuple[str, ...] = (
    "Step 1: Start AdGuard Home",
    "Step 2: Enable Zone 3 in PuffPAI Config",
    "Step 3: Test Safely (No Router Changes) - Option A",
)

#: Exactly what the owner runs, in order, to bring Zone 3 up themselves. These
#: are the lines ``install.sh --with-zone3`` prints; the wizard repeats them and
#: runs none of them. Placeholders are spelled out because a command with a
#: guessed path in it is worse than one the owner has to complete.
ZONE3_APPLY_COMMANDS: tuple[str, ...] = (
    "./install.sh --with-zone3",
    "set -a; . <your .env>; set +a",
    "docker compose -f docker-compose.zone3.yml up -d",
    "<venv>/bin/python scripts/zone3_bootstrap_adguard.py",
)

#: Zone 3 starts WATCHING, not blocking. The engine's own word for it.
ZONE3_MONITOR_MODE = "monitor-first"


def edge_options(support: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The edge-security menu, with Zone 3's availability folded in.

    ``support`` is :func:`puffpai.setup_ops.zone3_support`'s verdict for THIS
    machine. When it says no, the Zone 3 row still appears - greyed, with the
    reason printed - because a menu that silently drops an option teaches the
    owner that the option does not exist. It is marked ``available: False`` and
    carries the plain sentence saying why.
    """
    verdict = dict(support or {})
    supported = bool(verdict.get("supported"))
    reasons = [str(r) for r in (verdict.get("reasons") or [])]

    kinds: list[dict[str, Any]] = []
    for kind in EDGE_KINDS:
        copy = _EDGE_COPY[kind]
        row: dict[str, Any] = {
            "id": kind,
            "label": copy["label"],
            "description": copy["description"],
            "available": True,
            "reasons": [],
        }
        if kind == "zone3":
            row["available"] = supported if support is not None else True
            row["reasons"] = reasons
            row["mode"] = ZONE3_MONITOR_MODE
            row["doc"] = ZONE3_DOC
            row["doc_sections"] = list(ZONE3_DOC_SECTIONS)
            row["commands"] = list(ZONE3_APPLY_COMMANDS)
        kinds.append(row)

    return {
        "question": "What protects your network edge today?",
        "kinds": kinds,
        "external_kinds": [{"id": k, "label": v} for k, v in EXTERNAL_EDGE_KINDS],
        "default": DEFAULT_EDGE_KIND,
        "note": (
            "Whatever you answer, the Bridge posture above is configured. "
            "Nothing here is applied by the wizard - Zone 3 prints the commands "
            "for you to run, and your own gear is only written down."
        ),
    }


def edge_manifest_block(kind: str = DEFAULT_EDGE_KIND, *,
                        external_kind: str = "",
                        external_url: str = "",
                        external_url_note: str = "",
                        support: Optional[dict[str, Any]] = None,
                        ) -> dict[str, Any]:
    """The owner's edge answer, shaped for ``manifest['bridge']['edge']``.

    ``declared`` is always True: this block exists only because the step ran and
    the owner answered, so a reader can tell "there is nothing at the edge" from
    "nobody was ever asked". That distinction is the whole reason the step is
    not skipped for people with no gear.

    Zone 3 is recorded as REQUESTED, never as running. ``zone3.supported``
    carries this machine's verdict and ``zone3.reasons`` the plain sentences
    behind it; ``zone3.mode`` is set only when the request can actually be
    carried out, because a mode on a thing that cannot start is a fiction.
    Never raises; never contacts anything.
    """
    chosen = str(kind or "").strip().lower()
    if chosen not in EDGE_KINDS:
        chosen = DEFAULT_EDGE_KIND

    verdict = dict(support or {})
    supported = bool(verdict.get("supported"))
    requested = chosen == "zone3"

    zone3: dict[str, Any] = {
        "requested": requested,
        "supported": supported,
        "reasons": [str(r) for r in (verdict.get("reasons") or [])],
        "mode": ZONE3_MONITOR_MODE if (requested and supported) else "",
        "commands": list(ZONE3_APPLY_COMMANDS) if requested else [],
        "doc": ZONE3_DOC if requested else "",
        "doc_sections": list(ZONE3_DOC_SECTIONS) if requested else [],
        "applied": False,
    }

    summary = _EDGE_COPY[chosen]["summary"]
    if requested and not supported:
        first = zone3["reasons"][0] if zone3["reasons"] else (
            "This machine cannot run Zone 3.")
        summary = (f"You asked for HomeBot's own network edge, and it cannot run "
                   f"here. {first} Nothing was started and nothing changed.")

    return {
        "kind": chosen,
        "declared": True,
        "label": _EDGE_COPY[chosen]["label"],
        "summary": summary,
        "external_kind": str(external_kind or "").strip() if chosen == "external" else "",
        "external_url": str(external_url or "").strip() if chosen == "external" else "",
        "external_url_note": str(external_url_note or "") if chosen == "external" else "",
        "contacted": False,
        "zone3": zone3,
    }


def edge_summary_lines(edge: dict[str, Any]) -> list[str]:
    """Plain ASCII lines describing the recorded edge answer, for the CLI."""
    block = edge or {}
    lines = [f"  Network edge: {block.get('label') or block.get('kind')}"]
    lines.append(f"    {block.get('summary', '')}")
    if block.get("kind") == "external":
        if block.get("external_kind"):
            lines.append(f"    Kind: {block['external_kind']}")
        if block.get("external_url"):
            lines.append(f"    Admin page (recorded, never opened): "
                         f"{block['external_url']}")
        if block.get("external_url_note"):
            lines.append(f"    {block['external_url_note']}")
    zone3 = block.get("zone3") or {}
    if zone3.get("requested"):
        if zone3.get("supported"):
            lines.append(f"    Starts in {zone3.get('mode')} mode: it watches "
                         f"and does not block until you say so.")
            lines.append("    Run these yourself when you mean it:")
            lines.extend(f"      {cmd}" for cmd in zone3.get("commands") or [])
            lines.append(f"    Read first: {zone3.get('doc')} - "
                         f"{'; '.join(zone3.get('doc_sections') or [])}")
        else:
            for reason in zone3.get("reasons") or []:
                lines.append(f"    {reason}")
    return lines


def _postures_path() -> str:
    """Absolute path of the shipped posture descriptions."""
    from puffpai.config import get_config
    return os.path.join(get_config().REPO_ROOT, "config", "bridge_postures.json")


def load_postures(path: Optional[str] = None) -> dict[str, Any]:
    """Load the posture copy, degrading to the baked-in fallback on any failure.

    A file that does not describe all four postures is treated as unusable: the
    Bridge step offers the full ladder or it offers the trustworthy fallback,
    never a partial menu that quietly hides the strict end of it.
    """
    target = path or _postures_path()
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("postures"), list):
            ids = {p.get("id") for p in data["postures"] if isinstance(p, dict)}
            if ids == set(_STRICTNESS):
                return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return _FALLBACK_POSTURES


def posture_menu(postures: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """The four postures, loosest first, each with its code-owned strictness."""
    data = postures or load_postures()
    menu: list[dict[str, Any]] = []
    for entry in data.get("postures") or []:
        if not isinstance(entry, dict):
            continue
        pid = str(entry.get("id", ""))
        if pid not in _STRICTNESS:
            continue
        item = dict(entry)
        # Strictness is ours, not the file's.
        item["strictness"] = _STRICTNESS[pid]
        item["recommendable"] = pid not in _NEVER_RECOMMENDED
        menu.append(item)
    menu.sort(key=lambda item: item["strictness"])
    return menu


def posture_by_id(posture: str, postures: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Return one posture's entry, or None if the id is not one of the four."""
    for entry in posture_menu(postures):
        if entry.get("id") == posture:
            return entry
    return None


# ---------------------------------------------------------------------------
# recommendation
# ---------------------------------------------------------------------------

def posture_for_environment(
    has_home_assistant: bool,
    has_external_firewall: bool,
    has_managed_lan: bool,
    postures: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Suggest one posture for what the owner says they have, and show all four.

    The three inputs are answers the owner gave, not anything this module found
    out. The result is a suggestion the wizard displays next to the full menu; it
    is never applied on its own, which is why the returned dict always carries
    the whole ladder and says so in ``note``.

    Open Yard is never the suggestion. Someone who wants it can pick it, but the
    wizard does not talk anybody into the one posture that removes the second
    chance to say no. Never raises.
    """
    home_assistant = bool(has_home_assistant)
    firewall = bool(has_external_firewall)
    managed_lan = bool(has_managed_lan)

    if home_assistant:
        recommended = "fenced"
        reason = (
            "You have Home Assistant, so your bot needs to reach devices in the "
            "house. Fenced lets it do that while still gating anything that "
            "reaches further."
        )
    elif managed_lan or firewall:
        gear = "a managed network" if managed_lan else "a firewall of your own"
        recommended = "fenced"
        reason = (
            f"You already run {gear}, which handles the edge of your network. "
            f"Fenced covers the part that gear does not: what your bot itself is "
            f"allowed to do once it is inside."
        )
    else:
        recommended = "moat"
        reason = (
            "There is no firewall and no managed network in front of this box, "
            "so your bot's own rules are the only rules. With Moat, only the "
            "outside services you switched on reach the internet, and your "
            "home network still works."
        )

    return {
        "recommended": recommended,
        "reason": reason,
        "default": DEFAULT_POSTURE,
        "never_recommended": sorted(_NEVER_RECOMMENDED),
        "environment": {
            "home_assistant": home_assistant,
            "external_firewall": firewall,
            "managed_lan": managed_lan,
        },
        "postures": posture_menu(postures),
        "note": (
            "This is a suggestion, not a setting. The Bridge is not configured "
            "until you pick one of the four yourself."
        ),
    }


# ---------------------------------------------------------------------------
# reconciliation with Step 0's run_egress
# ---------------------------------------------------------------------------

def reconcile_with_egress(posture: str, run_egress: str) -> tuple[str, str]:
    """Reconcile a Bridge posture with the build's ``run_egress``. Stricter wins.

    A sealed build forces Drawbridge Up; an island build will not sit behind Open
    Yard. Returns ``(posture, explanation)`` where the explanation is always a
    plain sentence - describing the override when there was one, and confirming
    the choice when there was not, so the wizard can print it either way.

    Fails closed twice over: an unrecognised posture becomes the default rather
    than the loosest option, and an unrecognised ``run_egress`` applies the
    default's floor rather than no floor at all. Never loosens, ever.
    """
    chosen = str(posture or "").strip().lower()
    egress = str(run_egress or "").strip().lower()

    known_posture = chosen in _STRICTNESS
    if not known_posture:
        chosen = DEFAULT_POSTURE

    floor = _EGRESS_FLOOR.get(egress, _UNKNOWN_EGRESS_FLOOR)
    result = chosen if _STRICTNESS[chosen] >= _STRICTNESS[floor] else floor

    notes: list[str] = []
    if not known_posture:
        notes.append(
            f"We did not recognise that Bridge posture, so we used "
            f"{_label(DEFAULT_POSTURE)}, the safe default. Pick one on purpose "
            f"before you finish."
        )
    if egress not in _EGRESS_FLOOR:
        notes.append(
            "We did not recognise the build's egress setting, so we held the "
            "Bridge at the safe default rather than assuming the loose one."
        )

    if result != chosen:
        if egress == "sealed":
            notes.append(
                f"You chose a build that keeps everything on this computer, so "
                f"the Bridge is set to {_label('drawbridge_up')}. The stricter "
                f"of your two choices always wins."
            )
        else:
            notes.append(
                f"{_label(chosen)} would allow more than your {egress or 'chosen'} "
                f"build does, so the Bridge is set to {_label(result)} instead. "
                f"The stricter of your two choices always wins."
            )
    elif known_posture and egress in _EGRESS_FLOOR:
        notes.append(
            f"{_label(result)} fits {_article(egress)} {egress} build, "
            f"so nothing was changed."
        )

    return result, " ".join(notes)


def _article(word: str) -> str:
    """"a" or "an" for ``word``. Small thing, but "a island" reads as sloppy
    in the one screen where we are asking someone to trust our judgment."""
    return "an" if (word or "").lower()[:1] in "aeiou" else "a"


def _label(posture: str) -> str:
    """Display label for a posture id, falling back to the id itself."""
    entry = posture_by_id(posture)
    if entry and entry.get("label"):
        return str(entry["label"])
    return posture


# ---------------------------------------------------------------------------
# manifest block
# ---------------------------------------------------------------------------

def bridge_manifest_block(
    posture: str,
    *,
    run_egress: Optional[str] = None,
    environment: Optional[dict[str, Any]] = None,
    chosen_by: str = "owner",
    configured_at: Optional[float] = None,
    postures: Optional[dict[str, Any]] = None,
    edge: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Record the Bridge policy for the manifest.

    ``edge`` is :func:`edge_manifest_block`'s answer and lands verbatim under
    ``edge``. It is filled in even when the owner has no gear at all, because
    "there is nothing at my edge" is an answer and the manifest records answers.

    When ``run_egress`` is given the posture is reconciled first, so a manifest
    can never record a Bridge looser than the build's egress tier allows - the
    record is the reconciled truth, not what was asked for.

    Carries no key that could be read back as a switch on the contracts (the
    vault, the consent gate, the action gates, zero telemetry). It survives
    :func:`puffpai.build_manifest.sanitize_manifest` unchanged. Never raises.
    """
    requested = str(posture or "").strip().lower()
    if run_egress is not None:
        final, note = reconcile_with_egress(requested, str(run_egress))
    elif requested in _STRICTNESS:
        final, note = requested, ""
    elif requested in RETIRED_POSTURES:
        final, note = RETIRED_POSTURES[requested], MOAT_FOLDED_NOTE
    else:
        final = DEFAULT_POSTURE
        note = (
            f"We did not recognise that Bridge posture, so we recorded "
            f"{_label(DEFAULT_POSTURE)}, the safe default."
        )

    entry = posture_by_id(final, postures) or {}
    env = environment or {}
    source = str(chosen_by).strip().lower()
    if source not in _CHOSEN_BY:
        source = "owner"
    if requested != final:
        source = "override"

    return {
        "posture": final,
        "label": entry.get("label", final),
        "summary": entry.get("summary", ""),
        "allows": dict(entry.get("allows") or {}),
        "costs": entry.get("costs", ""),
        "strictness": _STRICTNESS[final],
        "run_egress": str(run_egress).strip().lower() if run_egress is not None else None,
        "requested_posture": requested or None,
        "reconciled": requested != final,
        "note": note,
        "environment": {
            "home_assistant": bool(env.get("home_assistant")),
            "external_firewall": bool(env.get("external_firewall")),
            "managed_lan": bool(env.get("managed_lan")),
        },
        "edge": dict(edge) if edge else edge_manifest_block(),
        "chosen_by": source,
        "enforced_by": "Drawbridge",
        "configured_at": configured_at if configured_at is not None else time.time(),
    }


def migrate_record(block: Any) -> dict[str, Any]:
    """An older Bridge record, read as today's. Never raises, never loosens.

    A record whose posture is in :data:`RETIRED_POSTURES` becomes the posture
    it folds into, with :data:`MOAT_FOLDED_NOTE` as its note and today's
    wording. Every other record is returned unchanged (as a copy);
    :func:`current_words` is what a page shows.
    """
    out = dict(block) if isinstance(block, dict) else {}
    old = str(out.get("posture") or "").strip().lower()
    if old not in RETIRED_POSTURES:
        return out
    new = RETIRED_POSTURES[old]
    entry = posture_by_id(new) or {}
    out.update({
        "posture": new,
        "label": entry.get("label", new),
        "summary": entry.get("summary", ""),
        "allows": dict(entry.get("allows") or {}),
        "costs": entry.get("costs", ""),
        "strictness": _STRICTNESS[new],
        "note": MOAT_FOLDED_NOTE,
        "migrated_from": old,
    })
    return out


def current_words(block: Any) -> dict[str, Any]:
    """A Bridge record with TODAY's words for its posture. Never raises.

    A saved record carries the sentences it was written with; a page shows
    the current ones, so it never repeats wording that has since changed. A
    retired posture is migrated first; an unknown posture is returned as it
    was (a copy).
    """
    out = migrate_record(block)
    pid = str(out.get("posture") or "").strip().lower()
    if pid in _STRICTNESS:
        entry = posture_by_id(pid) or {}
        out.update({
            "posture": pid,
            "label": entry.get("label", pid),
            "summary": entry.get("summary", ""),
            "allows": dict(entry.get("allows") or {}),
            "costs": entry.get("costs", ""),
            "strictness": _STRICTNESS[pid],
        })
    return out


# ---------------------------------------------------------------------------
# display
# ---------------------------------------------------------------------------

def posture_summary_lines(recommendation: dict[str, Any]) -> list[str]:
    """Plain-language lines describing the Bridge menu, for the CLI/API."""
    lines: list[str] = []
    rec = recommendation.get("recommended")
    for entry in recommendation.get("postures") or []:
        mark = " <- suggested" if entry.get("id") == rec else ""
        lines.append(f"  - {entry.get('label')}{mark}: {entry.get('summary')}")
        lines.append(f"      Costs you: {entry.get('costs')}")
    if rec:
        lines.append(f"Suggested: {_label(str(rec))}")
        lines.append(f"  {recommendation.get('reason', '')}")
    if recommendation.get("note"):
        lines.append(f"  {recommendation['note']}")
    return lines


# ---------------------------------------------------------------------------
# enforcement: what the running bot may actually reach
# ---------------------------------------------------------------------------

#: The home network is allowed; each outside service waits for its switch.
LEVEL_OPEN = "open"
#: Only the outside services the owner switched on reach the internet; the
#: home network still works.
LEVEL_SWITCHED_ON = "switched_on"
#: Nothing reaches the internet, switched on or not. The home network still
#: works, except on a build whose ``run_egress`` is ``sealed``
#: (``home_network`` False in :func:`enforced_from_manifest`).
LEVEL_SEALED = "sealed"

#: The level each posture is enforced at. Every posture is listed, so a
#: posture added to :data:`_STRICTNESS` without a level here fails loudly.
_POSTURE_LEVEL: dict[str, str] = {
    "open_yard": LEVEL_OPEN,
    "fenced": LEVEL_OPEN,
    "moat": LEVEL_SWITCHED_ON,
    "drawbridge_up": LEVEL_SEALED,
}

#: A service with no row of its own on "What leaves your computer" that is
#: used only as part of another one. With only switched-on services allowed it
#: follows that row's switch: the locked box's setup and its code parts are
#: downloads of code the owner picked, so they wait for "Downloading an app or
#: code you picked" to be switched on.
SWITCH_FOLLOWS: dict[str, str] = {"box_install": "code_download"}

#: What the Bridge does not hold (security.bridge_guard, "What it does not
#: do"): a name lookup asks the resolver before any connection is judged, and
#: a UDP datagram sent without connecting is not seen. Said wherever a line
#: says what the bot connects to, so no line claims more than is enforced.
NAME_LOOKUP_LINE = ("Looking up a website's address still asks your network; "
                    "the Bridge does not stop that.")

#: One plain sentence per level, for the pages and the terminal. Only what is
#: enforced: connections (security.bridge_guard), not name lookups.
ENFORCED_LINES: dict[str, str] = {
    LEVEL_OPEN: ("Your bot may reach things on your home network. Each outside "
                 "service has its own switch in Settings, Bridge switches."),
    LEVEL_SWITCHED_ON: ("Only the outside services you switched on reach the "
                        "internet. Your home network still works: Home "
                        "Assistant, your media server, your phones and your "
                        "other devices at home. " + NAME_LOOKUP_LINE),
    LEVEL_SEALED: ("Nothing reaches the internet. Your home network still "
                   "works: Home Assistant, your media server, your phones and "
                   "your other devices at home. " + NAME_LOOKUP_LINE),
}

#: The line for a build whose ``run_egress`` is ``sealed``: not even the home
#: network is reached.
LOCAL_ONLY_LINE = ("Your bot keeps everything on this computer: it connects to "
                   "nothing on your home network or the internet. "
                   + NAME_LOOKUP_LINE)

#: What the owner reads when the Bridge stopped something.
REFUSED_LINE = ("The Bridge is closed to this because it was not switched on, "
                "so nothing was sent.")
SEALED_LINE = ("Your bot is set so that nothing reaches the internet, so "
               "nothing was sent.")
LOCAL_ONLY_REFUSED_LINE = ("Your bot is set to keep everything on this "
                           "computer, so nothing was sent.")


def _run_egress_of(block: Any) -> str:
    """``run_egress`` from a manifest's ``posture`` block, normalised the way
    :func:`puffpai.build_posture.assess_posture` does: an unknown value is the
    ordinary default (``island``), never a looser one."""
    raw = (str((block or {}).get("run_egress") or "").strip().lower()
           if isinstance(block, dict) else "")
    return raw if raw in _EGRESS_FLOOR else "island"


def enforced_from_manifest(manifest: Any) -> dict[str, Any]:
    """The Bridge as the running bot enforces it, from a manifest dict.

    Pure: reads nothing, opens nothing. ``None`` (no manifest yet: no wizard
    has run) reads as the default. A manifest that is not a dict, or a Bridge
    value nobody knows, reads as ``drawbridge_up``. The run setting's floor
    then applies: a sealed build is never looser than ``drawbridge_up``, and
    its level is ``sealed``. ``home_network`` says whether the home network
    may be reached: True at every Bridge setting, False only on a build whose
    run setting is ``sealed`` (this computer only). The cached reading from
    disk that the running bot uses is
    :func:`puffpai.security.bridge_guard.enforced`.
    """
    posture_block: Any = {}
    if manifest is None:
        chosen, source = DEFAULT_POSTURE, "default"
    elif not isinstance(manifest, dict):
        chosen, source = "drawbridge_up", "unreadable"
    else:
        bridge = manifest.get("bridge")
        posture_block = manifest.get("posture")
        source = "record"
        if bridge is None:
            chosen, source = DEFAULT_POSTURE, "default"
        elif not isinstance(bridge, dict):
            chosen = "drawbridge_up"
        else:
            raw = str(bridge.get("posture") or "").strip().lower()
            if not raw:
                chosen, source = DEFAULT_POSTURE, "default"
            elif raw in RETIRED_POSTURES:
                chosen = RETIRED_POSTURES[raw]
            elif raw in _STRICTNESS:
                chosen = raw
            else:
                chosen = "drawbridge_up"
    run_egress = _run_egress_of(posture_block)
    floor = _EGRESS_FLOOR.get(run_egress, _UNKNOWN_EGRESS_FLOOR)
    final = chosen if _STRICTNESS[chosen] >= _STRICTNESS[floor] else floor
    local_only = run_egress == "sealed"
    level = LEVEL_SEALED if local_only else _POSTURE_LEVEL[final]
    return {"posture": final, "chosen": chosen, "run_egress": run_egress,
            "level": level, "home_network": not local_only, "source": source,
            "line": LOCAL_ONLY_LINE if local_only else ENFORCED_LINES[level]}


def switch_allows_at(level: str, sid: str, get_switch) -> bool:
    """Does the Bridge at *level* let *sid* be used, given the switches?

    *get_switch* answers True / False / None (never set) for a service id,
    as :func:`puffpai.security.outside_switches.get` does. Every outside
    service ships switched off, so at levels ``open`` and ``switched_on``
    only an explicit ON (or the ON of the row it follows,
    :data:`SWITCH_FOLLOWS`) lets it be used; the owner's yes in the wizard or
    in Settings is that ON. ``sealed``: nothing. An unknown level is read as
    ``switched_on``. The levels still differ in what
    :mod:`puffpai.security.bridge_guard` lets through at connect time.
    """
    got = get_switch(sid)
    if got is False or level == LEVEL_SEALED:
        return False
    if got is True:
        return True
    follow = SWITCH_FOLLOWS.get(str(sid or "").strip().lower())
    return bool(follow) and get_switch(follow) is True


__all__ = [
    "DEFAULT_EDGE_KIND",
    "DEFAULT_POSTURE",
    "EDGE_KINDS",
    "ENFORCED_LINES",
    "EXTERNAL_EDGE_KINDS",
    "LEVEL_OPEN",
    "LEVEL_SEALED",
    "LEVEL_SWITCHED_ON",
    "LOCAL_ONLY_LINE",
    "LOCAL_ONLY_REFUSED_LINE",
    "MOAT_FOLDED_NOTE",
    "NAME_LOOKUP_LINE",
    "REFUSED_LINE",
    "RETIRED_POSTURES",
    "SEALED_LINE",
    "SWITCH_FOLLOWS",
    "ZONE3_APPLY_COMMANDS",
    "ZONE3_DOC",
    "ZONE3_DOC_SECTIONS",
    "ZONE3_MONITOR_MODE",
    "bridge_manifest_block",
    "current_words",
    "edge_manifest_block",
    "edge_options",
    "edge_summary_lines",
    "enforced_from_manifest",
    "load_postures",
    "migrate_record",
    "posture_by_id",
    "posture_for_environment",
    "posture_menu",
    "posture_summary_lines",
    "reconcile_with_egress",
    "switch_allows_at",
]
