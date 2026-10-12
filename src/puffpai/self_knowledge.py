"""What this build cannot do, and why - answered from the build manifest.

The wizard already decided every limitation this module reports. A build that
runs a small brain cannot plan; a sealed build cannot reach the network; a
stargazing bot never loaded the camera surface; a vault that is sealed needs a
PIN. All of it is recorded in ``config/build_manifest.json`` by the time setup
finishes. Nothing here detects anything new. It reads the record and turns it
into plain words.

Why this exists at all: a bot that cannot do something has three ways to fail,
and two of them are terrible. It can raise an opaque error, it can quietly
pretend it complied ("Sure, I turned on the lights"), or it can say what it is
and what it is not. The third is the only acceptable one, and hallucinated
compliance is the worst thing a local assistant can do to the person trusting
it. The smaller the brain, the more often it happens - which means the builds
that need this most are exactly the ones running on a stranger's old laptop.

The rule that makes it work: **ask before dispatch, not after failure.** A
request for a surface this build never loaded is answered here, honestly, and
never reaches a model that would improvise compliance.

Voice: a limitation is a fact plus a door, never an apology. Say what the build
is, not what it failed to be, and where a limit can be lifted, say how.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

from typing import Any, Optional

# Why a capability is unavailable. Ordered most-specific first: when several
# apply, the most actionable explanation is the one worth saying out loud.
CAUSE_NOT_CONFIGURED = "not_configured"
CAUSE_SURFACE_NOT_LOADED = "surface_not_loaded"
CAUSE_EGRESS_SEALED = "egress_sealed"
CAUSE_EGRESS_LAN_ONLY = "egress_lan_only"
CAUSE_NO_PLANNING = "no_planning"
CAUSE_VAULT_SEALED = "vault_sealed"
CAUSE_NO_CREW = "no_crew"
CAUSE_CONFIG_DEGRADED = "config_degraded"

# Surfaces whose whole point is reaching past this box. Asking for one of these
# on a sealed build is a posture answer, not a surface answer.
_WAN_SURFACES = frozenset({
    "knowledge.web", "media.streaming", "comms.mail",
})


def _manifest(manifest: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The committed manifest, or an empty dict. Never raises."""
    if manifest is not None:
        return manifest
    try:
        from puffpai import build_manifest
        return build_manifest.read_manifest() or {}
    except Exception:
        return {}


def _trimming_is_active(surfaces: dict[str, Any]) -> bool:
    """True when this build actually has a surface allowlist.

    A freshly assembled manifest carries an empty allowlist because the build
    interview has not run yet. That is "not trimmed", not "nothing allowed" -
    reading it the other way would make a stock build claim it cannot do
    anything, which is a lie in the opposite direction.
    """
    if surfaces.get("derived_from_goal") or surfaces.get("owner_edited"):
        return True
    return bool(surfaces.get("allowlist"))


def _model_id(man: dict[str, Any]) -> Optional[str]:
    brain = man.get("brain") or {}
    chosen = brain.get("model")
    if chosen:
        return str(chosen)
    try:
        from puffpai.config import get_config
        return str(get_config().ollama.model)
    except Exception:
        return None


def can(capability: str,
        manifest: Optional[dict[str, Any]] = None) -> tuple[bool, str]:
    """Can this build do ``capability``, and if not, why - in plain words.

    ``capability`` is a surface id from the taxonomy (``house.cameras``,
    ``instruments.telescope``), or one of the behaviour names ``plan``,
    ``reach_wan``, ``reach_lan``, ``read_vault``.

    Returns ``(True, "")`` when available, else ``(False, reason)`` where the
    reason names the cause AND the door out of it. Never raises.
    """
    try:
        return _can(capability, _manifest(manifest))
    except Exception:
        # Self-knowledge failing must never take a request down with it. When
        # we cannot tell, say we cannot tell rather than guessing either way.
        return (True, "")


def _can(capability: str, man: dict[str, Any]) -> tuple[bool, str]:
    if not man:
        return (True, "")

    cap = (capability or "").strip().lower()
    posture = man.get("posture") or {}
    egress = str(posture.get("run_egress") or "").lower()
    surfaces = man.get("surfaces") or {}
    allowlist = set(surfaces.get("allowlist") or [])

    # --- behaviour questions -------------------------------------------
    if cap == "plan":
        model = _model_id(man)
        if model:
            from puffpai.model_profile import supports
            if not supports(model, "planning"):
                return (False, why(CAUSE_NO_PLANNING, model=model))
        return (True, "")

    if cap == "reach_wan":
        if egress == "sealed":
            return (False, why(CAUSE_EGRESS_SEALED))
        if egress == "island":
            return (False, why(CAUSE_EGRESS_LAN_ONLY))
        return (True, "")

    if cap == "reach_lan":
        if egress == "sealed":
            return (False, why(CAUSE_EGRESS_SEALED))
        return (True, "")

    if cap == "read_vault":
        return (False, why(CAUSE_VAULT_SEALED))

    # --- surface questions ----------------------------------------------
    if cap in _WAN_SURFACES and egress in ("sealed", "island"):
        cause = CAUSE_EGRESS_SEALED if egress == "sealed" else CAUSE_EGRESS_LAN_ONLY
        return (False, why(cause))

    if _trimming_is_active(surfaces) and cap and cap not in allowlist:
        known = _is_known_surface(cap)
        if known:
            return (False, why(CAUSE_SURFACE_NOT_LOADED, surface=cap,
                               goal=_build_goal(man)))
    return (True, "")


def _build_goal(man: dict[str, Any]) -> Optional[str]:
    """The owner's stated goal, if this build recorded one.

    Checked in order of specificity. The manifest does not always carry it -
    the surfaces block records the derivation, not the sentence - so callers
    must cope with ``None`` and say the generic thing instead of inventing a
    goal the owner never typed.
    """
    surfaces = man.get("surfaces") or {}
    goal = surfaces.get("goal")
    if goal:
        return str(goal)
    name = (man.get("build") or {}).get("name")
    return str(name) if name else None


def _is_known_surface(surface: str) -> bool:
    """True when the id is a real surface in the shipped taxonomy."""
    try:
        from puffpai.build_interview import surface_taxonomy
        tax = surface_taxonomy() or {}
        return surface in tax
    except Exception:
        return False


def why(cause: str, **detail: Any) -> str:
    """The plain-words explanation for a cause. Fact plus a door, no apology."""
    surface = detail.get("surface") or "that"
    if cause == CAUSE_SURFACE_NOT_LOADED:
        goal = detail.get("goal")
        because = (f" This build was set up for {goal}, so that surface was "
                   f"never loaded.") if goal else \
                  " That surface was not part of this build."
        return (f"I do not have {surface}.{because} "
                f"You can add it by running setup again.")
    if cause == CAUSE_EGRESS_SEALED:
        return ("I cannot reach anything off this box. This build is sealed - "
                "that was the choice at setup, and it is doing its job. "
                "Re-run setup to open specific doors.")
    if cause == CAUSE_EGRESS_LAN_ONLY:
        return ("I can reach things on your own network but not the internet. "
                "Re-run setup if you want to open a specific door.")
    if cause == CAUSE_NO_PLANNING:
        return ("I do one step at a time on this machine and I do not chain "
                "them together. Ask me for one thing and I will do it well. "
                "A bigger brain than this hardware fits would chain them.")
    if cause == CAUSE_VAULT_SEALED:
        return ("That is in your vault, so I need your PIN. Even I have to "
                "ask.")
    if cause == CAUSE_NO_CREW:
        return ("This machine runs the core team only, with no room for extra "
                "specialists. That is a size limit, not a setting.")
    if cause == CAUSE_CONFIG_DEGRADED:
        return ("Part of my setup could not be read, so I am working from a "
                "smaller built-in list and I am missing options I should have.")
    if cause == CAUSE_NOT_CONFIGURED:
        return ("I have not been set up yet. Run setup and I will know what I "
                "am.")
    return "That is not something this build does."


def explain_refusal(intent: str,
                    manifest: Optional[dict[str, Any]] = None) -> str:
    """What to say instead of pretending. Use this in place of a fake success.

    ``intent`` is a surface id or behaviour name. Always returns something
    sayable, even when the build turns out to be capable after all - in that
    case the caller had a different problem and should say so plainly.
    """
    ok, reason = can(intent, manifest)
    if not ok:
        return reason
    return ("I should be able to do that, and it did not work. That is a "
            "fault on my side, not something you did wrong.")


def limits_summary(manifest: Optional[dict[str, Any]] = None) -> list[str]:
    """The honest self-description: what this build is and is not.

    Returned as ASCII lines so the CLI, the first-run walkthrough, and the
    dashboard can all
    print it. Capability and limitation are stated together, never separately -
    a list of only limits reads as an apology and a list of only features
    reads as a brochure.
    """
    man = _manifest(manifest)
    if not man:
        return [why(CAUSE_NOT_CONFIGURED)]

    lines: list[str] = []
    model = _model_id(man)

    if model:
        try:
            from puffpai.model_profile import limits_sentence, routing_tier
            lines.append(f"Brain: {model} (tier: {routing_tier(model)}).")
            sentence = limits_sentence(model)
            if sentence:
                lines.append(sentence)
        except Exception:
            lines.append(f"Brain: {model}.")

    posture = man.get("posture") or {}
    egress = str(posture.get("run_egress") or "").lower()
    if egress == "sealed":
        lines.append("Reach: sealed build, no outside service switched on. "
                     "Nothing leaves this box without your consent.")
    elif egress == "island":
        lines.append("Reach: your own network only. No internet.")
    elif egress:
        doors = posture.get("enabled_switches") or []
        lines.append("Reach: only the Bridge switches you opened - "
                     + (", ".join(doors) if doors else "none yet") + ".")

    surfaces = man.get("surfaces") or {}
    if _trimming_is_active(surfaces):
        allow = sorted(surfaces.get("allowlist") or [])
        lines.append("I carry: " + (", ".join(allow) if allow else "the core only")
                     + ". Anything else was not loaded, on purpose.")

    # Only speak to the crew when crew sizing actually ran. An unconfigured
    # manifest defaults to zero specialists, and reporting that as a hardware
    # limit turns a question nobody answered into a limitation nobody has.
    crew = man.get("crew") or {}
    max_spec = crew.get("max_specialists")
    if crew.get("master_core") is not None and isinstance(max_spec, int):
        if max_spec <= 0:
            lines.append(why(CAUSE_NO_CREW))
        else:
            lines.append(f"Crew: room for up to {max_spec} specialists "
                         f"beyond the core team.")

    try:
        from puffpai import config_health
        if config_health.is_degraded():
            lines.append(why(CAUSE_CONFIG_DEGRADED))
    except Exception:
        pass

    return [ln for ln in lines if ln]


def capabilities(manifest: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Machine-readable view: what is loaded, what is not, and why not."""
    man = _manifest(manifest)
    surfaces = man.get("surfaces") or {}
    try:
        from puffpai.build_interview import surface_taxonomy
        tax = list((surface_taxonomy() or {}).keys())
    except Exception:
        tax = []

    available: list[str] = []
    unavailable: dict[str, str] = {}
    for surface in tax:
        ok, reason = can(surface, man)
        if ok:
            available.append(surface)
        else:
            unavailable[surface] = reason

    return {
        "configured": bool(man),
        "trimmed": _trimming_is_active(surfaces),
        "available": sorted(available),
        "unavailable": unavailable,
        "can_plan": can("plan", man)[0],
        "can_reach_wan": can("reach_wan", man)[0],
        "summary": limits_summary(man),
    }


__all__ = [
    "can", "why", "explain_refusal", "limits_summary", "capabilities",
    "CAUSE_NOT_CONFIGURED", "CAUSE_SURFACE_NOT_LOADED", "CAUSE_EGRESS_SEALED",
    "CAUSE_EGRESS_LAN_ONLY", "CAUSE_NO_PLANNING", "CAUSE_VAULT_SEALED",
    "CAUSE_NO_CREW", "CAUSE_CONFIG_DEGRADED",
]
