"""«What automations do I have?» — the UNION of the three places they live.

Left to the model, this question can get "I don't have any personal
automations configured" after 25 seconds of model time, while the appliance
holds Home Assistant automations and scripts, saved workflows in the Majordomo
task store, and a Node-RED runtime with flows on it.

The cause is structural, not a bad model: without a single reader that owns
this question, whichever store the planner happens to look at decides the
answer, and a store it does not look at reads as "none". A false negative about the
household's own automations is the worst shape this can take, so the rule here
is: the answer is a UNION, and "none configured" is only speakable when every
source is readable AND empty.

Pure. The caller collects; this module counts and words it.
"""

from __future__ import annotations

import re

#: "what automations do I have" and the ways a household asks it. A WRITE
#: phrasing ("create an automation that ...") is excluded: that is the request
#: the standing answer says no to, and it must reach its own path, not a list.
_QUESTION_RE = re.compile(
    r"(?i)^\s*(?:hey[,\s]+)?(?:so\s+|and\s+)?"
    r"(?:what|which|how\s+many|do\s+i\s+have\s+any|list|show\s+me|tell\s+me)"
    r"\b[^?.!]{0,40}?"
    r"\b(?:automations?|automated\s+routines?|routines?|workflows?|"
    r"node[-\s]?red\s+flows?|flows?|scheduled\s+tasks?)\b")

#: "set up" is a write verb in "set up an automation" and a PASSIVE in "what
#: automations are set up" — so the object, not the verb alone, decides.
_WRITE_RE = re.compile(
    r"(?i)\b(?:create|build|author)\b"
    r"|\b(?:make|add|set\s+up|write)\s+(?:me\s+)?(?:a|an|another|some|new)\b"
    r"|\bnew\s+(?:automation|workflow|flow|routine)s?\b")


def is_automations_question(text) -> bool:
    """True for a READ of the household's automations. Pure."""
    body = str(text or "").strip()
    if not body or _WRITE_RE.search(body):
        return False
    return bool(_QUESTION_RE.match(body))


#: Names spoken aloud, across all kinds.
MAX_NAMES = 5

#: The standing honest boundary: the
#: planner still does not author Node-RED flows, but PuffPAI now drafts a new
#: Home Assistant automation for the owner's approval
#: (:mod:`puffpai.ha_automation_builder`).
BOUNDARY = ("I run saved ones, and I can draft a new Home Assistant automation "
            "for you to approve.")


def _names(items) -> list:
    out = []
    for item in (items or []):
        if isinstance(item, dict):
            name = (item.get("name") or item.get("friendly_name")
                    or item.get("label") or item.get("intent")
                    or item.get("id") or item.get("entity_id"))
        else:
            name = item
        name = str(name or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def _count_phrase(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def reply_for(ha_automations=None, ha_scripts=None, saved=None,
              nodered=None, nodered_readable: bool = True) -> str:
    """The union, counted by kind, with up to five names.

    ``nodered_readable=False`` means the Node-RED admin API could not be read.
    Then the flows are mentioned WITHOUT a count rather than counted as zero —
    an unreadable source is not an empty one, and conflating the two is exactly
    the false negative this module exists to prevent.
    """
    automations = _names(ha_automations)
    scripts = _names(ha_scripts)
    saved_names = _names(saved)
    flows = _names(nodered)

    parts = []
    if automations:
        parts.append(_count_phrase(len(automations), "Home Assistant automation",
                                   "Home Assistant automations"))
    if scripts:
        parts.append(_count_phrase(len(scripts), "script", "scripts"))
    if saved_names:
        parts.append(_count_phrase(len(saved_names), "saved workflow",
                                   "saved workflows"))
    if nodered_readable:
        if flows:
            parts.append(_count_phrase(len(flows), "Node-RED flow",
                                       "Node-RED flows"))
    else:
        parts.append("your Node-RED flows")

    if not parts:
        return ("Nothing is set up right now — no Home Assistant automations "
                "or scripts, no saved workflows and no Node-RED flows. "
                + BOUNDARY)

    if len(parts) == 1:
        counted = parts[0]
    else:
        counted = ", ".join(parts[:-1]) + " and " + parts[-1]

    names = (automations + scripts + saved_names + flows)[:MAX_NAMES]
    sentence = f"You have {counted}."
    if names:
        sentence += " " + ("They include: " if len(names) > 1 else "It's: ") \
            + ", ".join(names) + "."
    return sentence + " " + BOUNDARY
