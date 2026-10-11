"""«Can you do a system health check?» → the caretaker probe, never the library.

Routed to the library, "can you do a system health check" gets an
explanation of what a health check IS, citing a software guide, in 15
seconds. The appliance has a real probe, the Caretaker's
:meth:`run_health_checks`, and an ops question routed to the knowledge
shelves is one of the more embarrassing things this appliance can do.

Deterministic and spoken-length: services up/down, whether the knowledge store
is ready, whether the voice models are installed, and how many checks failed.
Pure — the caller runs the probe and hands the report in.
"""

from __future__ import annotations

import re

_QUESTION_RE = re.compile(
    r"(?i)^\s*(?:hey[,\s]+)?(?:can\s+you\s+|could\s+you\s+|please\s+|"
    r"could\s+you\s+please\s+|run\s+a\s+|do\s+a\s+|give\s+me\s+a\s+)?"
    r"(?:"
    r"(?:do|run|perform|give\s+me)\s+(?:a\s+)?(?:full\s+|quick\s+)?"
    r"system\s+(?:health\s+check|health|status|check)"
    r"|(?:do|run|perform)\s+(?:a\s+)?(?:full\s+|quick\s+)?health\s+check"
    r"|health\s+check"
    r"|system\s+health"
    r"|system\s+status"
    r"|status\s+(?:report|check)"
    r"|(?:are|is)\s+(?:all\s+)?(?:your|the)\s+services\s+running"
    r"|is\s+everything\s+(?:running|working|ok|okay|up)"
    r"|are\s+you\s+(?:all\s+)?(?:working|running|ok|okay|healthy)"
    r"|how\s+are\s+your\s+services"
    r")"
    r"\b[^?.!]{0,24}[?.!]*\s*$")

#: The probe's own service ids, in the words a household uses for them.
SERVICE_LABEL = {
    "ollama": "the language models",
    "chromadb": "the knowledge store",
    "api_server": "the API",
    "nodered": "Node-RED",
    "jellyfin": "the media server",
}


def is_health_question(text) -> bool:
    """True for an OPS question about this box. Pure."""
    return bool(_QUESTION_RE.match(str(text or "").strip()))


def _label(service) -> str:
    return SERVICE_LABEL.get(str(service), str(service))


def _join(items) -> str:
    items = list(items)
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def reply_for(report, voice=None) -> str:
    """Word the caretaker report. ``voice`` is ``{ready, missing}`` or None.

    A report that could not be produced says so; it never degrades into "all
    good", which is the shape of a health check that is worse than none.
    """
    if not isinstance(report, dict) or not report.get("ok"):
        return ("I couldn't run the health probe just now, so I can't tell you "
                "whether everything is up — that's the honest answer, not a "
                "clean bill of health.")
    checks = [c for c in (report.get("checks") or []) if isinstance(c, dict)]
    up = [_label(c.get("service")) for c in checks if c.get("reachable")]
    down = [_label(c.get("service")) for c in checks if not c.get("reachable")]

    if down:
        head = (f"{len(up)} of {len(checks)} services are up. "
                f"Down: {_join(down)}.")
    else:
        head = f"All {len(checks)} services are up."

    knowledge = next((c for c in checks
                      if str(c.get("service")) == "chromadb"), None)
    if knowledge is not None:
        head += (" The knowledge store is ready."
                 if knowledge.get("reachable")
                 else " The knowledge store isn't ready.")

    if isinstance(voice, dict):
        if voice.get("ready"):
            head += " Voice is available."
        else:
            missing = voice.get("missing") or []
            head += (" Voice isn't fully available"
                     + (f" — missing {_join([str(m) for m in missing])}."
                        if missing else "."))

    failures = len(down)
    head += (" No failures." if not failures
             else f" {failures} check{'s' if failures > 1 else ''} failed.")
    return head


def handle(user_message, report, voice=None) -> "str | None":
    if not is_health_question(user_message):
        return None
    return reply_for(report, voice)
