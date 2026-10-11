"""Concierge — WF-1 conversation (fronts the user, holds no secrets).

The Concierge is the only agent the user talks to. It classifies each message
with the existing keyword :mod:`~puffpai.router`, then either answers directly
(via its LLM engine, a :class:`PuffPAIAgent`) or delegates to the agent that owns
the capability — Steward for the house, Archivist for memory/knowledge,
Majordomo for scheduled work — *through the bus*.

Crucially, its scope is empty (``AgentScope.CONCIERGE``): a prompt-injected or
misbehaving Concierge cannot read the HA token or the vault, because
:meth:`~puffpai.agents.base.BaseAgent.secret` would raise ``SecretAccessDenied``.
It can only ask. That is the whole point of the split.

Requester identity (``envelope.requester``) is forwarded on every delegated
envelope so the downstream agent can enforce consent + privacy mode (e.g. a kid
profile hitting the Archivist gets Zone-2 only).

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from puffpai.ha_bridge import is_valid_entity_id
from puffpai.logging_config import get_logger
from puffpai.router import RouteTarget, get_route_category, route_message
from puffpai.security.secrets_provider import AgentScope, SecretsProvider

from .base import BaseAgent
from .caretaker import DEFAULT_SELF_UNIT, RESTARTABLE_UNITS
from .envelope import Budget, Envelope
from .planning_loop import PlanningLoop, planning_loop_enabled
from .steward import HUMAN_ORIGINS, STATE_CHANGING_INTENTS

logger = get_logger(__name__)

#: ``origin_agent`` values whose text may be RESOLVED INTO A HOUSEHOLD COMMAND.
#: Everything else that reaches :meth:`ConciergeAgent._converse` is prose to be
#: answered, never a request to actuate — see :meth:`ConciergeAgent._prose_only`.
#:
#: Deliberately a superset of :data:`~puffpai.agents.steward.HUMAN_ORIGINS` and
#: NOT that set itself. The dashboard/voice chat front door stamps
#: ``"api_server"`` on purpose *because* it is outside ``HUMAN_ORIGINS``: the
#: Steward's always-confirm siren gate keys on that set, so chat can propose a
#: siren but never fire one. Gating resolution on ``HUMAN_ORIGINS`` would stop
#: every real user message from reaching a device, and widening ``HUMAN_ORIGINS``
#: to fix that would hand chat the siren. So the two questions — "may this
#: actuate without confirmation" and "may this become a command at all" — get
#: two sets.
#:
#: Fail-CLOSED: membership is the allowlist, so an origin nobody thought about
#: (the ``Envelope`` default ``"system"``, the dispatcher, a queue-worker
#: envelope, an agent added next year) is answered rather than obeyed. An agent
#: stamps its own ``self.name``, so it cannot talk its way into this set without
#: lying about its identity.
RESOLVING_ORIGINS = HUMAN_ORIGINS | {"api_server"}

#: Intents that are conversational BY NAME, whatever their origin: the payload
#: text is material to work over, never an instruction to follow. Kept separate
#: from the origin check so an internal caller can be explicit at its own call
#: site instead of relying on the Concierge to infer intent from provenance.
PROSE_ONLY_INTENTS = frozenset({"summarize"})

# Route-category → agent the Concierge delegates to.
_CATEGORY_TO_AGENT = {
    "home": "steward",
    "media": "steward",
    "app": "steward",
    "library": "archivist",
    "rag": "archivist",
    "briefing": "majordomo",
    "security": "sentinel",
}

# Night window (local time) for camera judgement: a person at the shed at 02:00
# is not the same event as a person at the shed at 14:00.
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 6


# ── operational self-healing ───────────────────────────────────────
# The Node-RED ``health_check`` tab reports *display* names ("PuffPAI API"); the
# Caretaker's allowlist speaks *unit* names ("puffpai-api"). This closed table is
# the only bridge between the two. An unrecognised name is never guessed at — it
# stays "unknown", which is neither core (so it cannot raise severity on its own)
# nor restartable (so it can never produce a restart suggestion).
SERVICE_NAME_TO_KEY = {
    "puffpai api": "puffpai-api",
    "puffpai-api": "puffpai-api",
    "api_server": "puffpai-api",
    "puffpai dashboard": "puffpai-dashboard",
    "puffpai-dashboard": "puffpai-dashboard",
    "dashboard": "puffpai-dashboard",
    "ollama llm": "ollama",
    "ollama": "ollama",
    "node-red": "puffpai-nodered",
    "puffpai-nodered": "puffpai-nodered",
    "home assistant": "home-assistant",
    "homeassistant": "home-assistant",
}

#: PuffPAI's own moving parts. One of these down is a PuffPAI fault → attention.
CORE_SERVICES = frozenset({
    "puffpai-api", "puffpai-dashboard", "ollama", "puffpai-nodered",
})

#: Services PuffPAI *talks to* but does not own. A house routinely has offline
#: hardware (a large share of HA entities can be offline after a network
#: change), so Home Assistant being unreachable is news, not an emergency, and
#: there is nothing on this appliance to restart that would fix it.
PERIPHERAL_SERVICES = frozenset({"home-assistant"})

#: How long an *identical* set of down services stays deduplicated. A 5-minute
#: cron would otherwise write 288 identical entries a day into the event log and
#: the audit ledger. 30 minutes is three Caretaker rate-limit windows
#: (``MIN_RESTART_INTERVAL_SECONDS`` = 600s), so a suppressed repeat can never be
#: hiding a fault the appliance was about to be allowed to act on anyway.
SERVICE_DEGRADED_DEDUP_SECONDS = 1800.0

#: down-set → monotonic timestamp it was last reported. Module level on purpose:
#: dedup must hold across every producer and every Concierge instance, and events
#: arrive on background threads.
_degradation_seen: dict[frozenset, float] = {}
_degradation_lock = threading.Lock()


def _monotonic() -> float:
    """Monotonic clock. Indirected so tests can pin the dedup window."""
    return time.monotonic()


def reset_service_degraded_dedup() -> None:
    """Forget every remembered degradation (recovery, and test setup)."""
    with _degradation_lock:
        _degradation_seen.clear()


def _note_and_check_duplicate(fingerprint: frozenset) -> bool:
    """Record *fingerprint*; return True when it was already reported recently.

    THE dedup mechanism for event deciders — there is deliberately only one.
    ``service_degraded`` owns it and ``self_check``
    reuses it rather than growing a second table with its own window, lock and
    reset function. Fingerprints are namespaced by their producer so the two
    event types can never collide in the shared table, and the aging pass keeps
    the table bounded whichever producer touched it last.
    """
    now = _monotonic()
    with _degradation_lock:
        last = _degradation_seen.get(fingerprint)
        duplicate = (last is not None
                     and (now - last) < SERVICE_DEGRADED_DEDUP_SECONDS)
        if not duplicate:
            _degradation_seen[fingerprint] = now
            # Bound the table: at most one entry per distinct fingerprint, and
            # stale ones (recovered faults) age out.
            for key, seen in list(_degradation_seen.items()):
                if now - seen >= SERVICE_DEGRADED_DEDUP_SECONDS:
                    _degradation_seen.pop(key, None)
    return duplicate


def _forget_duplicates(prefix: str) -> None:
    """Drop remembered fingerprints belonging to one producer (recovery).

    Same table, same lock as :func:`_note_and_check_duplicate` — the scoped
    counterpart of :func:`reset_service_degraded_dedup`, so a clean self-check
    cannot erase what the service-degraded decider is remembering.
    """
    with _degradation_lock:
        for key in [k for k in _degradation_seen
                    if any(str(e).startswith(prefix) for e in k)]:
            _degradation_seen.pop(key, None)


#: Self-check ids whose failure is a fact about the *house*, not a fault in the
#: appliance, and must never raise severity on their own. Devices going quiet in
#: groups is what happens when a hub loses its network (this house lost 128
#: entities at once on an ISP change); an un-PIN'd vault is a normal fresh-box
#: state that only a human can resolve. Both are reported, neither is alarming.
SELF_CHECK_INFORMATIONAL = frozenset({
    "ha_entities_unavailable",
    "vault_uninitialized",
})


def service_key(name) -> str:
    """Canonical key for a health-check service name; ``""`` when unrecognised."""
    if not isinstance(name, str):
        return ""
    return SERVICE_NAME_TO_KEY.get(name.strip().lower(), "")


# ── deterministic intent resolution ─────────────────────────────────
# Hardcoding ``list_devices`` for every "home" message would answer "take a
# snapshot from the porch camera" with a device list. Resolution is
# *keyword/pattern first* and runs with no LLM attached:
# chat arrives on request threads that may hit a cold or absent model, and an
# LLM that has to be up before the house responds is not a house control system.
# ``_engine_refine_intent`` may narrow the fallback when an engine is attached,
# but nothing here depends on it.


@dataclass
class Resolution:
    """One resolved specialist intent, before entity resolution."""

    intent: str | None
    #: HA domains the target must live in ( () = any domain ).
    domains: tuple[str, ...] = ()
    payload: dict = field(default_factory=dict)
    needs_entity: bool = False
    reason: str = ""

    @property
    def state_changing(self) -> bool:
        return self.intent in STATE_CHANGING_INTENTS


_WORD_RE = re.compile(r"[a-z0-9]+")
_ENTITY_IN_TEXT_RE = re.compile(r"\b([a-z_]+\.[a-z0-9_]+)\b")

#: Cues that a request is more than one action, and therefore worth the
#: planning loop's latency. Narrow on purpose — see ``_should_plan``.
_MULTI_STEP_RE = re.compile(
    r"(\band then\b|\bthen\b|\bafter that\b|\bafterwards\b|\balso\b|"
    r"\bfirst\b.*\bsecond\b|;)", re.IGNORECASE)

#: Splits a request into candidate action segments. Deliberately BROAD where
#: :data:`_MULTI_STEP_RE` is narrow: a bare "and" or a comma may split here,
#: because each segment is then re-resolved and only counts when it names a
#: genuinely different intent. The guard against over-planning lives in
#: :meth:`ConciergeAgent._spans_multiple_actions`, not in this pattern.
_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:\band\b|\bplus\b|,|;)\s*", re.IGNORECASE)

# ── is this turn an ANSWER, or a fresh request? ──────────────────────────────
#: A pending clarification is a half-finished sentence between the planner and
#: a person, and the next thing they type is the second half of it only if it
#: reads like an answer. When it reads like a NEW errand, folding it into the
#: old goal is how one unresolvable device name latched onto every device
#: question that followed it: each turn re-planned "turn on the shed light and
#: photograph the shed", got the same question back, and re-armed the slot with
#: a fresh timestamp — so the TTL never expired and only a restart cleared it.
#: See :meth:`ConciergeAgent._answers_the_ask`.

#: Replies that are answers whatever else they look like. Checked FIRST,
#: because "do it" and "don't" open with auxiliaries and would otherwise read
#: as questions.
_ASK_REPLY_RE = re.compile(
    r"^(?:yes|yeah|yep|yup|sure|ok|okay|correct|right|affirmative"
    r"|no|nope|nah|don'?t|cancel|stop|forget\s+it|never\s*mind|nevermind"
    r"|do\s+it|do\s+both|both|either|neither|go\s+ahead|please\s+do)\b"
    r"[^?]*\??\s*$",
    re.IGNORECASE)

#: Phrases that only exist because a question was just asked. They are what
#: makes "do the porch one" an ANSWER and "how many devices can you control" a
#: new errand, even though both open with an auxiliary: an answer points BACK
#: at the question. Without this the question rule below swallowed most real
#: replies — "do the second one", "how about the porch one", "what i meant was
#: the porch light" — and dropped the goal they were answering.
_ASK_REFERENT_RE = re.compile(
    # "the porch one", "that one", "which one", "the second one"
    r"\b(?:the|that|this|which|either|another|other|first|second|third|last)\b"
    r"(?:\s+\w+){0,2}\s+ones?\b"
    r"|\bi\s+(?:meant|mean|said)\b|\byou\s+mean\b"
    r"|\b(?:how|what)\s+about\b"
    r"|\binstead\b|\brather\b"
    # "is it the porch?" points back; "how warm is it in here" does not, so
    # this one is anchored. A bare ``\bis\s+it\b`` matched both.
    r"|^is\s+it\b",
    re.IGNORECASE)

#: How many words still counts as a bare noun phrase. A short reply that ends
#: in a question mark ("the porch light?", "porch?") is someone naming the
#: thing and checking they got it right -- an answer. Only a turn long enough
#: to BE a question gets read as one on the strength of its punctuation.
_ASK_REPLY_MAX_WORDS = 5

#: Polite REQUEST forms. "can you do the porch light" opens like a question and
#: is a reply by function, so it must not be mistaken for a fresh errand.
_POLITE_REQUEST_RE = re.compile(
    r"^(?:please\b|(?:can|could|would|will)\s+you\b)",
    re.IGNORECASE)

#: A question is a request of its own. Someone answering "which shed light?"
#: types a NAME, not another question — so "is light.x still on" and "how many
#: devices can you control" are errands, not the second half of anything.
_QUESTION_OPENER_RE = re.compile(
    r"^(?:who|whos|what|whats|when|where|why|how|hows|which|whose"
    r"|is|are|was|were|do|does|did|can|could|will|would|should|shall"
    r"|has|have|had|am|may|might)\b",
    re.IGNORECASE)

# Rule order matters — first match wins. Narrow/unambiguous phrasings come
# before the broad ones ("turn on the siren" must not land on control_device).
_EXPLICIT_LIST_RE = re.compile(
    r"\b(?:list|show|what)\s+(?:my\s+|the\s+|all\s+)*devices\b"
    r"|\bdiscover\s+devices\b|\bwhat\s+can\s+you\s+control\b")
_SIREN_RE = re.compile(r"\bsiren\b|\bsound\s+the\s+alarm\b"
                       r"|\bset\s+off\s+the\s+alarm\b")
_SNAPSHOT_RE = re.compile(
    r"\b(?:snapshots?|snaps?)\b"
    # "picture(s)/pic(s)/photo(s)/image(s)" are camera words UNLESS they name
    # files on disk ("rename my photo files", "batch-rename these images") —
    # that is a coding/automation ask, not a camera ask. See
    # tests/test_camera*.py and tests/test_code_model.py.
    r"|\b(?:pictures?|pics?|photo(?:graph)?s?|images?)\b"
    r"(?!\s+files?\b)"
    # "still" only counts as a noun. Bare \bstill\b matched the far commoner
    # adverb, so "is the shed light still on" resolved to a camera snapshot.
    r"|\ba\s+stills?\b|\bstills?\s+(?:image|frame|shot|from|of)\b"
    r"|\b(?:show|see|look\s+at|check|pull\s+up|view)\b[^.?!]{0,40}\bcam(?:era)?s?\b"
    r"|\bcam(?:era)?s?\b[^.?!]{0,20}\b(?:feed|view|footage)\b")
_LIST_ADD_RE = re.compile(
    r"\badd\s+(?:a\s+|an\s+|some\s+)?(?P<item>.+?)\s+to\s+(?:the\s+|my\s+|our\s+)?"
    r"(?P<which>shopping|grocery|groceries|todo|to-do|to\s+do|task)\s*list\b")
_ANNOUNCE_RE = re.compile(
    r"\bannounce\b|\bbroadcast\b|\btell\s+(?:the\s+house|everyone|everybody)\b"
    r"|\bsay\s+(?:to\s+the\s+house|out\s+loud)\b")
_NOTIFY_RE = re.compile(
    r"\bnotify\s+me\b|\bsend\s+me\s+a\s+notification\b"
    r"|\bpush\s+(?:me\s+)?a\s+notification\b|\blet\s+me\s+know\s+(?:that|when|about)\b")
_STATUS_RE = re.compile(
    r"\bwhat(?:'s| is)\s+the\s+(?:state|status)\b|\b(?:state|status)\s+of\b"
    r"|^\s*(?:is|are)\s+(?:the|my)\b[^.?!]*\b(?:on|off|open|closed|locked|"
    r"unlocked|running|home)\b"
    # "did I leave the garage door open?" is a read too. Without this it fell
    # to the list_devices default, which answers a different question.
    r"|\bdid\s+(?:i|we|anyone|someone|somebody)\s+leave\b[^.?!]*"
    r"\b(?:on|off|open|closed|unlocked|running)\b")
#: Read-only sensor questions — "what is the temperature in the living room",
#: "how warm is it in here". These reach the team (RAG has no way to see a
#: thermostat and would say so). They are
#: *reads*, so they resolve before ``_CLIMATE_RE``, whose setpoint branch would
#: otherwise claim any phrasing carrying a two-digit number. Domains are
#: narrowed to climate+sensor so the name match cannot land on the living-room
#: *lamp*, which shares every room token with the thermostat.
_TEMP_READ_RE = re.compile(
    r"\bwhat(?:'s| is|\s+is)?\s+(?:the\s+)?(?:current\s+)?"
    r"(?:temperature|temp|humidity)\b"
    r"|\bhow\s+(?:warm|cold|hot|humid)\s+is\s+it\b"
    r"|\b(?:temperature|humidity)\s+reading\b")
_CLIMATE_RE = re.compile(
    r"\bthermostat\b|\bair\s*condition\w*\b|\bset\s+the\s+(?:temp|temperature|heat|ac|a/c)\b"
    r"|\b(?:make\s+it|turn\s+it)\s+(?:warmer|cooler|hotter|colder)\b"
    r"|\b(?:warmer|cooler)\b|\b\d{2,3}\s*(?:degrees|°)")
_SCENE_RE = re.compile(
    r"\bscene\b|\b(?:movie|chill|party|bedtime|good ?night|dinner|reading)\s+"
    r"(?:mode|night|time)\b")
_MEDIA_STATUS_RE = re.compile(
    r"\b(?:now|currently)\s+playing\b|\bwhat(?:'s| is)\s+playing\b"
    r"|\bmedia\s+(?:server\s+)?status\b")
_MEDIA_SEARCH_RE = re.compile(
    r"\b(?:search|find|look\s+for|browse|what)\b[^.?!]{0,30}"
    r"\b(?:movies?|films?|music|songs?|albums?|artists?|books?|audiobooks?)\b"
    r"|\bdo\s+i\s+have\b[^.?!]{0,30}\b(?:movies?|music|books?)\b")
_MEDIA_PLAY_RE = re.compile(r"\b(?:play|watch|resume|shuffle)\b")
_CONTROL_RE = re.compile(
    r"\bturn\s+(?:on|off)\b|\bswitch\s+(?:on|off)\b|\btoggle\b|\bdim\b"
    r"|\bbrighten\b|\bset\s+(?:the\s+)?brightness\b")
#: Locks and covers need ``lock.lock`` / ``cover.open_cover``; the Steward's
#: ``control_device`` only speaks turn_on/turn_off/toggle, so these stay legacy.
_UNSUPPORTED_CONTROL_RE = re.compile(
    r"\b(?:un)?lock\s+the\b|\b(?:open|close)\s+the\s+"
    r"(?:garage|door|blinds?|shades?|curtains?|cover|gate)\b")

#: Zigbee pairing — a human-initiated fast path, matched FIRST.
#:
#: First because the pairing phrasings sit close to the broad rules below
#: ("discover devices", "what … devices") and a request to open the house's
#: radio to strangers must not be resolved by whichever generic rule happens to
#: match a synonym.
#:
#: Narrow on purpose, but not narrower than the way people speak. ``pair`` is
#: unconditional; "add"/"connect"/"join" are admitted only when nothing is being
#: added *to* a container, because those verbs otherwise collide with
#: ``list_add`` and with the camera and dashboard flows. "onboard" is still
#: refused. The status rule requires either the word "pairing" or
#: an utterance that *ends* at "what did you find" — a bare "what did you find
#: in my documents" is a knowledge question and must stay one.
#:
#: Neither intent is in :data:`RESOLVABLE_INTENTS`: that set is the menu the
#: optional LLM refinement path may choose from, and the model may not decide to
#: open a pairing window. These rules are the only route to either intent.
_ZIGBEE_PAIR_STATUS_RE = re.compile(
    r"\bpairing\s+status\b|\bstatus\s+of\s+(?:the\s+)?pairing\b"
    # "did the zigbee pairing window open" is a question about the window,
    # not a request to open one - if it matched nothing here it would fall
    # through to the device resolver and read two raw script entity ids out
    # loud. A past-tense / interrogative frame around the pairing window is
    # always a status question; the REQUEST forms all put the verb first
    # ("open the pairing window"), which _ZIGBEE_PAIR_RE still owns.
    r"|\b(?:did|has|have|was|were|is)\b[^.?!]{0,40}\bpairing\s+window\b"
    r"|\bpairing\s+window\b[^.?!]{0,20}\b(?:open(?:ed)?|close[d]?|work(?:ed)?)\b"
    r"|\bwhat\s+(?:did\s+you|happened)\b[^.?!]{0,30}\bpair(?:ing|ed)?\b"
    r"|\bdid\s+(?:you|puff)\s+(?:find|see|spot|catch)\s+(?:any\s+)?"
    r"(?:new\s+)?(?:zigbee\s+)?(?:devices?|bulbs?|lights?|switches?)\b"
    r"|\bany\s+new\s+(?:zigbee\s+)?(?:devices?|bulbs?)\b"
    r"|\bwhat\s+did\s+you\s+find\b(?:\s+when\s+pairing)?\s*[?.!]*\s*$")
#: Determiners and hardware adjectives allowed between the pairing verb and the
#: noun. A bounded alternation, deliberately not ``.*?``: an open gap would read
#: "add milk to the list and check the light" as a request to open the house's
#: radio to strangers. The list must include "a"/"an": without them "pair a
#: new zigbee bulb in the living room" matches nothing here, falls through to
#: the optional LLM refinement, and can come back classified as
#: ``set_climate``, so the reply is a thermostat disambiguation and the
#: Steward's consent path is never reached.
_PAIR_FILLER = (
    r"(?:a|an|another|my|the|some|these|those|any|new|more|second|spare|"
    r"extra|smart|zigbee|z-?wave|zwave|wi-?fi|light|"
    # Without these, "pair both zigbee bulbs" matches nothing here and goes
    # to device control ("I couldn't find a device matching that").
    r"both|all|of|our|two|three|four|five|six|\d{1,2})\s+")
#: The hardware a pairing window gets opened for. Longest forms first so
#: "light bulb" is consumed whole rather than as the adjective "light".
_PAIR_NOUN = (
    r"(?:lightbulbs?|light\s+bulbs?|bulbs?|lights?|devices?|switch(?:es)?|"
    r"plugs?|sensors?|dongles?)")
_ZIGBEE_PAIR_RE = re.compile(
    r"\bpair(?:ing)?\s+mode\b"
    r"|\bopen\s+(?:the\s+)?pairing\s+window\b"
    r"|\bput\s+(?:the\s+)?(?:zigbee\s+)?(?:network|hub|coordinator)\s+"
    r"in(?:to)?\s+pairing\b"
    # "pair a new zigbee" — the radio is named and the noun left implicit.
    rf"|\bpair(?:ing)?\s+(?:{_PAIR_FILLER}){{0,4}}(?:zigbee|z-?wave|zwave)\b"
    rf"|\bpair(?:ing)?\s+(?:{_PAIR_FILLER}){{0,4}}{_PAIR_NOUN}\b"
    # Weaker verbs. The trailing ``(?!\s+to\b)`` is the whole guard: "add a
    # bulb TO the shopping list" and "add lights TO the scene" are writes owned
    # by other rules, while "add a new zigbee bulb" is onboarding. Because "|"
    # binds loosest, that lookahead applies to this branch alone — ``pair``
    # above keeps the unconditional precedence it has always had.
    rf"|\b(?:add|connect|join)\s+(?:{_PAIR_FILLER}){{0,4}}{_PAIR_NOUN}\b"
    r"(?!\s+to\b)")

_TEMP_RE = re.compile(r"\b(\d{2,3})\s*(?:degrees|deg|°|f\b)?")
_BRIGHTNESS_RE = re.compile(r"\b(\d{1,3})\s*(?:%|percent)"
                            r"|\b(?:brightness|dim(?:med)?)\b[^\d]{0,30}(\d{1,3})")
_SAY_TEXT_RE = re.compile(
    r"(?:announce|broadcast|say|tell\s+(?:the\s+house|everyone|everybody))\s*"
    r"(?:that\s+)?[:,]?\s*(?P<text>.+)$")
_NOTIFY_TEXT_RE = re.compile(
    r"(?:notify\s+me|send\s+me\s+a\s+notification|let\s+me\s+know)\s*"
    r"(?:that\s+|about\s+|when\s+)?[:,]?\s*(?P<text>.+)$")

#: Intents the Concierge is willing to resolve. Anything outside this set is
#: left to the legacy path (the team has no hands for it yet).
RESOLVABLE_INTENTS = frozenset({
    "list_devices", "device_status", "camera_snapshot", "control_device",
    "activate_scene", "set_climate", "siren", "list_add", "announce",
    "notify_user", "media_status", "media_search",
})


def resolve_intent(text: str) -> Resolution:
    """Map free text to a Steward intent. Pure, deterministic, no I/O.

    Returns ``Resolution(intent=None)`` when the team has no hand for the
    request (e.g. media *playback*, which needs an item id the Steward has no
    resolver for) — the caller then leaves the turn to the legacy agent.
    """
    lower = (text or "").lower().strip()

    # Zigbee pairing first — status before the request, so "pairing status" is
    # never read as "open pairing mode". Neither needs an entity: the window is
    # a property of the coordinator, and the status is a report.
    if _ZIGBEE_PAIR_STATUS_RE.search(lower):
        return Resolution("zigbee_pair_status")
    if _ZIGBEE_PAIR_RE.search(lower):
        return Resolution("zigbee_pair")

    if _EXPLICIT_LIST_RE.search(lower):
        return Resolution("list_devices")

    if _SIREN_RE.search(lower):
        action = "turn_off" if re.search(r"\b(?:off|stop|silence|quiet)\b", lower) \
            else "turn_on"
        return Resolution("siren", ("siren",), {"action": action},
                          needs_entity=True)

    if _SNAPSHOT_RE.search(lower):
        return Resolution("camera_snapshot", ("camera",), needs_entity=True)

    m = _LIST_ADD_RE.search(lower)
    if m:
        which = m.group("which")
        shopping = which in ("shopping", "grocery", "groceries")
        return Resolution(
            "list_add", () if shopping else ("todo",),
            {"item": m.group("item").strip(), "list": "shopping" if shopping else "todo"},
            needs_entity=not shopping)

    if _ANNOUNCE_RE.search(lower):
        said = _SAY_TEXT_RE.search(text or "")
        return Resolution("announce", ("assist_satellite", "media_player"),
                          {"text": (said.group("text").strip() if said else text)})

    if _NOTIFY_RE.search(lower):
        said = _NOTIFY_TEXT_RE.search(text or "")
        return Resolution("notify_user", (),
                          {"message": (said.group("text").strip() if said else text)})

    # Status before climate/control: "what's the status of the thermostat" is a
    # read, not a setpoint change.
    if _STATUS_RE.search(lower):
        return Resolution("device_status", (), needs_entity=True)

    # A temperature/humidity *question* is a read of a climate or sensor
    # entity, never a setpoint change — resolved before _CLIMATE_RE.
    if _TEMP_READ_RE.search(lower):
        return Resolution("device_status", ("climate", "sensor"),
                          needs_entity=True)

    if _CLIMATE_RE.search(lower):
        payload: dict = {}
        temp = _TEMP_RE.search(lower)
        if temp:
            payload["temperature"] = float(temp.group(1))
        if re.search(r"\bheat\b|\bwarmer\b|\bhotter\b", lower):
            payload.setdefault("hvac_mode", "heat")
        elif re.search(r"\bcool\b|\bcooler\b|\bcolder\b|\bac\b|\ba/c\b", lower):
            payload.setdefault("hvac_mode", "cool")
        if not payload:
            # "the thermostat" with no number and no direction is a question.
            return Resolution("device_status", ("climate",), needs_entity=True)
        return Resolution("set_climate", ("climate",), payload, needs_entity=True)

    if _SCENE_RE.search(lower):
        return Resolution("activate_scene", ("scene",), needs_entity=True)

    if _MEDIA_STATUS_RE.search(lower):
        return Resolution("media_status")

    if _MEDIA_SEARCH_RE.search(lower):
        kind = "music"
        if re.search(r"\bmovies?\b|\bfilms?\b", lower):
            kind = "movies"
        elif re.search(r"\bbooks?\b|\baudiobooks?\b", lower):
            kind = "books"
        return Resolution("media_search", (), {"kind": kind, "query": text})

    if _MEDIA_PLAY_RE.search(lower):
        # Playback needs a resolved library item id; the Steward exposes no
        # search->id->play resolver, so this stays with the legacy media tools.
        return Resolution(None, reason="media playback needs an item id the "
                                       "Steward cannot resolve")

    if _UNSUPPORTED_CONTROL_RE.search(lower):
        return Resolution(None, reason="locks and covers need services the "
                                       "Steward's control_device does not expose")

    if _CONTROL_RE.search(lower):
        payload = {}
        if re.search(r"\bturn\s+off\b|\bswitch\s+off\b", lower):
            payload["action"] = "turn_off"
        elif re.search(r"\btoggle\b", lower):
            payload["action"] = "toggle"
        else:
            payload["action"] = "turn_on"
        bright = _BRIGHTNESS_RE.search(lower)
        if bright:
            pct = int(bright.group(1) or bright.group(2))
            if 0 <= pct <= 100:
                payload["kwargs"] = {"brightness_pct": pct}
        return Resolution("control_device", (), payload, needs_entity=True)

    # Safe default, exactly as before: show what the house has.
    return Resolution("list_devices", reason="no rule matched")


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


#: Device-TYPE words → the HA domains that type can live in. When a request
#: names a type ("light", "thermostat"), only those domains are candidates.
#: This is what stops "turn off the living room light" from actuating
#: ``climate.living_room`` (friendly name "Living Room" — a perfect two-token
#: match) in a house whose lamps have no matching entity: a state-changing
#: verb aimed at a "light" must never land on a thermostat because the
#: thermostat's NAME scored well. Lamps map to switch too — plug-driven lamps
#: are common. Deliberately absent: bare "switch" (it's usually the verb) and
#: room-ish words ("garage").
_DEVICE_TYPE_DOMAINS: dict[str, tuple[str, ...]] = {
    "light": ("light", "switch"), "lights": ("light", "switch"),
    "lamp": ("light", "switch"), "lamps": ("light", "switch"),
    "bulb": ("light", "switch"), "bulbs": ("light", "switch"),
    "thermostat": ("climate",), "climate": ("climate",),
    "ac": ("climate",), "hvac": ("climate",),
    "heat": ("climate",), "heater": ("climate",), "heating": ("climate",),
    # climate ONLY — including "sensor" here sent CONTROL actions at
    # sensor.bedroom_temperature ("Bedroom Temperature", a 2-token match that
    # outscores climate.bedroom "Upstairs Bedroom"), which Sentinel
    # then denied as unconsented. Climate entities answer temperature READS
    # fine (current_temperature attribute), and a sensor is never a valid
    # thing to actuate.
    "temperature": ("climate",),
    "reolink": ("camera",),
    "fan": ("fan",), "fans": ("fan",),
    "tv": ("media_player", "remote"), "television": ("media_player", "remote"),
    "speaker": ("media_player",), "speakers": ("media_player",),
    "camera": ("camera",), "cameras": ("camera",),
    "plug": ("switch",), "plugs": ("switch",),
    "outlet": ("switch",), "outlets": ("switch",),
    "siren": ("siren",),
    "lock": ("lock",), "locks": ("lock",),
    "curtain": ("cover",), "curtains": ("cover",),
    "blind": ("cover",), "blinds": ("cover",),
    "shade": ("cover",), "shades": ("cover",),
}


def _implied_domains(text_tokens: set[str]) -> tuple[str, ...]:
    """Domains implied by device-type words in the request; () when none."""
    domains: list[str] = []
    for token in sorted(text_tokens):
        for domain in _DEVICE_TYPE_DOMAINS.get(token, ()):
            if domain not in domains:
                domains.append(domain)
    return tuple(domains)


def _score_device(name_tokens: set[str], text_tokens: set[str]) -> tuple[int, float]:
    """Rank key for a candidate device: (tokens matched, fraction matched).

    Match count leads so "Porch Light" beats a device merely called "Light"
    for "turn on the porch light"; the fraction breaks ties toward the tighter
    name.
    """
    if not name_tokens:
        return (0, 0.0)
    hit = name_tokens & text_tokens
    return (len(hit), len(hit) / len(name_tokens))


def _now() -> datetime:
    """Local wall-clock. Indirected so tests can pin the hour."""
    return datetime.now()


def is_night(now: datetime | None = None) -> bool:
    """True inside the 22:00–06:00 local night window."""
    hour = (now or _now()).hour
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR


def _describe_state(state) -> str:
    """Render an entity state for prose using *known fields only*.

    The Steward hands back HA's raw entity payload. Never string-dump it into a
    summary: the summary is persisted to the event log and shown to the user, so
    an upstream response body must not ride along. Only ``state`` (and, when
    present, ``friendly_name``) are quoted, both length-capped.
    """
    if state is None:
        return ""
    if isinstance(state, dict):
        value = state.get("state")
        if value is None:
            return ""
        text = str(value)[:32]
        friendly = (state.get("attributes") or {}).get("friendly_name") \
            if isinstance(state.get("attributes"), dict) else None
        if friendly:
            return f"{text} ({str(friendly)[:64]})"
        return text
    if isinstance(state, (str, int, float, bool)):
        return str(state)[:32]
    return ""


#: Numeric attributes worth quoting back, in the order a person would say them.
#: A ``climate`` entity's *state* is its HVAC mode ("heat"), so "the thermostat
#: is heat" is not an answer to "what is the temperature" — the reading lives in
#: the attributes. Same allowlist discipline as :func:`_describe_state`: named
#: numeric fields only, never a string dump of an upstream payload.
_READING_FIELDS = (
    ("current_temperature", "currently", "°"),
    ("current_humidity", "humidity", "%"),
    ("temperature", "set to", "°"),
)


def _describe_reading(state) -> str:
    """Render a climate/sensor entity's numeric attributes, or ``""``."""
    if not isinstance(state, dict):
        return ""
    attrs = state.get("attributes")
    if not isinstance(attrs, dict):
        return ""
    bits = []
    for key, label, unit in _READING_FIELDS:
        value = attrs.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        bits.append(f"{label} {value:g}{unit}")
    return ", ".join(bits)


class ConciergeAgent(BaseAgent):
    """User-facing conversation + delegation front."""

    name = "concierge"
    scope = AgentScope.CONCIERGE

    def __init__(self, *, secrets: SecretsProvider | None = None,
                 engine=None) -> None:
        super().__init__(secrets=secrets)
        # ``engine`` is an optional PuffPAIAgent-like object with ``.run(query)``.
        # It is NOT constructed here (empty scope, cheap startup); inject it or
        # leave it None to run in pure-routing mode.
        self._engine = engine
        #: The planning loop, built lazily and only when the kill-switch is
        #: on. Injectable so tests can pin its limits.
        self._loop: PlanningLoop | None = None
        #: One-slot memory of a clarification the planning loop just asked:
        #: ``{"goal", "question", "ts"}``. One slot because there is one
        #: household chat surface; the TTL keeps a stale question from
        #: hijacking an unrelated request minutes later.
        self._pending_ask: dict | None = None

    # ── pending clarification (one slot, short TTL) ─────────────────
    #: How long an unanswered clarification stays live. Long enough to type
    #: (or say) an answer, short enough that the next errand starts clean.
    PENDING_ASK_TTL_SECONDS = 180.0

    #: How many times one clarification chain may re-ask. The TTL alone does
    #: not bound the slot: re-arming stamps a fresh ``ts``, so a planner that
    #: answers every answer with the same question keeps the slot alive
    #: forever. Two rounds is a conversation; a third is a loop, and the person
    #: is better served by a clean turn than by a question they have already
    #: answered twice.
    PENDING_ASK_MAX_ROUNDS = 2

    def has_pending_ask(self) -> bool:
        """True while a just-asked clarification is still fresh."""
        pending = self._pending_ask
        if not pending:
            return False
        if time.monotonic() - pending["ts"] > self.PENDING_ASK_TTL_SECONDS:
            self._pending_ask = None
            return False
        return True

    def _take_pending_ask(self) -> dict | None:
        """Pop the pending clarification if fresh — a question is answered once."""
        if not self.has_pending_ask():
            return None
        pending, self._pending_ask = self._pending_ask, None
        return pending

    @staticmethod
    def _answers_the_ask(text: str) -> bool:
        """Whether this turn is the ANSWER to the pending question.

        Freshness is not enough. Claiming the slot for whatever non-blank text
        arrives inside the TTL is right for "the porch light" and wrong for
        most of what a person might say next: after an unresolvable "turn on
        the shed light and photograph the shed" asks for the exact device name,
        EVERY device question, including one naming a valid entity id
        outright, would come back as that same question. Each turn would be
        folded into the stale goal, re-planned, asked again, and re-arm the
        slot with a new timestamp, so the TTL would never fire and only a
        service restart would clear it.

        The gate defaults to ANSWER and only steps aside for a turn that is
        visibly its own errand, because the cost is asymmetric: a fresh request
        misread as an answer costs one turn (the ``rounds`` cap and the TTL
        both end it), while an answer misread as a fresh request silently
        throws away the goal the person was in the middle of.

        Regex-only rules, in order:

        * A confirmation or refusal is an answer whatever it opens with.
          "do it", "don't" and "both" are auxiliaries or quantifiers by shape
          and answers by meaning, so they are settled first.
        * So is anything POINTING BACK at the question — "the porch one", "i
          meant the hall light", "how about the second". These open with the
          same auxiliaries a question does, and reading them as new errands is
          how a first draft of this gate broke the feature it was guarding.
        * A polite request ("can you do the porch light") is a reply by
          function even though it opens like a question -- UNLESS its verb
          resolves to something state-changing, which the next rule catches.
          "please turn on the porch light" is therefore read as a fresh
          errand, and answering a clarification that way loses the rest of the
          pending goal. That is the deliberate side the asymmetry falls on
          here and only here: chaining a NEW actuation onto a stale actuation
          goal would let the planner act on the old goal too, and a skipped
          step the person can ask for again beats a device moving on its own.
        * Otherwise a QUESTION is a request of its own. Nobody answers "which
          shed light?" with "how many devices can you control" — that is
          someone who moved on, and their errand deserves a clean turn.
        * An outright COMMAND names its own errand too. Re-issuing "turn on the
          shed light and photograph the shed" is a retry, not an answer, and it
          must re-plan from scratch rather than chain onto its own leftovers.

        Anything else — a bare device name, an entity id, a profile, a room —
        is what an answer actually looks like.
        """
        stripped = (text or "").strip()
        if not stripped:
            return False
        if _ASK_REPLY_RE.match(stripped) or _ASK_REFERENT_RE.search(stripped):
            return True
        if not _POLITE_REQUEST_RE.match(stripped):
            if _QUESTION_OPENER_RE.match(stripped):
                return False
            if stripped.endswith("?") and (
                    len(stripped.split()) > _ASK_REPLY_MAX_WORDS):
                return False
        found = resolve_intent(stripped)
        if found.state_changing and found.reason != "no rule matched":
            return False
        # ...and so is a multi-step one. The retry that actually latched on the
        # box -- the same "turn on the shed light and photograph the shed",
        # typed again -- resolves to ``camera_snapshot``, which is a READ, so
        # the state-changing rule above lets it through. What gives it away is
        # that it is a whole errand: two segments, two intents, one of them
        # acting. That is exactly :meth:`_spans_multiple_actions`, and it is
        # the right gate rather than :meth:`_should_plan`, which also fires on
        # every UNMATCHED request -- and a bare device name ("the porch light")
        # is unmatched, which is precisely what an answer looks like.
        if ConciergeAgent._spans_multiple_actions(stripped):
            return False
        return True

    @staticmethod
    def _human_only(answered_ask: dict) -> str:
        """The human-authored text of a clarification so far — no question.

        A pending ask stores three things, and only two of them a person wrote.
        ``question`` is **planner-authored prose**: ``human.ask`` declares a
        ``question`` parameter, so a model may put up to 500 characters of its
        own choosing there. Folding that into the string the next run resolves
        and seals from is a laundry chute — a plan that asks

            human.ask "Should I read https://attacker.example/payload for you?"

        put that host into the FOLLOWING run's :class:`EgressScope`, because the
        combined goal it was re-planned from contained the question. The user
        never had to agree: the stash is claimed by the next message inside its
        TTL whatever that message says, so "no thanks" sealed the host just as
        well as "yes".

        The planner still SEES its own question — it is in the goal, which is
        what gets re-planned — but only what a person typed decides which device
        is aimed at and which host may be reached. ``request`` accumulates
        across chained clarifications so a second round cannot re-open the hole
        the first one closed; ``goal`` is the fallback for a stash written
        before this key existed.
        """
        return str(answered_ask.get("request") or answered_ask.get("goal") or "")

    @staticmethod
    def _context_preamble(history) -> str:
        """A compact recent-conversation block for the PLANNER goal.

        The resolver and the router keep seeing the raw message — regexes must
        not match yesterday's words — but the planner is an LLM, and "check
        that temperature again" is only plannable if it can see which
        temperature the conversation was about. Bounded twice (pairs and
        chars) because a single assistant turn can be a 111-device list and
        the plan call runs in a 4096-token context.
        """
        if not isinstance(history, list):
            return ""
        lines = []
        for pair in history[-3:]:
            if not isinstance(pair, dict):
                continue
            user = str(pair.get("user") or "").strip()[:400]
            assistant = str(pair.get("assistant") or "").strip()[:400]
            if user:
                lines.append(f"User said: {user}")
            if assistant:
                lines.append(f"You replied: {assistant}")
        if not lines:
            return ""
        return ("Recent conversation (for reference only):\n"
                + "\n".join(lines) + "\n\nCurrent request: ")

    def planning_loop(self) -> PlanningLoop:
        """The bounded plan→act→observe→revise loop."""
        if self._loop is None:
            self._loop = PlanningLoop(self, engine=self._engine)
        return self._loop

    # ── request handling ────────────────────────────────────────────
    def handle(self, envelope: Envelope) -> dict:
        intent = envelope.intent
        p = envelope.payload or {}
        if intent in ("chat", "user_message", "ask") or intent in PROSE_ONLY_INTENTS:
            return self._converse(envelope)
        if intent == "event":
            return self._classify_event(envelope)
        return {"ok": False, "error": f"concierge: unknown intent {intent!r}"}

    def _prose_only(self, envelope: Envelope, text: str, p: dict) -> dict:
        """Answer the text. Resolve nothing, delegate nothing, touch nothing.

        The path for every envelope this agent must not read as a command: an
        internal origin (see :data:`RESOLVING_ORIGINS`) or an explicitly
        conversational intent (see :data:`PROSE_ONLY_INTENTS`).

        Two things are deliberately NOT done here, and both matter.

        The Majordomo's morning briefing asks this agent to summarise a report
        whose BODY quotes device readings — "Hallway Thermostat reads 68." Run
        through the resolver that sentence is a ``set_climate`` to 68F on
        ``climate.hallway_thermostat``, state-changing, dispatched: a background
        summarisation job would silently move the setpoint, because a temperature in
        a report someone asked *about* is indistinguishable from one someone
        asked *for* once it is just a string on the resolver's input. So: no
        :meth:`_resolve`, no :meth:`_delegate`, no planning loop.

        And the pending-ask slot is left ALONE — not consumed, not refreshed,
        not cleared. It holds a half-finished conversation with a person. If
        ``_converse`` claimed it for any non-blank text regardless of who was
        speaking, a briefing landing inside the 180s TTL would answer the
        human's question on their behalf, and :meth:`_human_only` would then
        prefix their earlier request onto the briefing prompt. The person's
        clarification is theirs; a background job may not spend it. Its TTL
        keeps running on its own.
        """
        answer = None
        if self._engine is not None:
            try:
                answer = self._engine.run(text, p.get("conversation_history"))
            except Exception as exc:
                logger.warning("concierge engine failed: %s", exc)
                answer = None
        logger.debug("prose-only turn (intent=%r origin=%r): resolver and "
                     "pending ask untouched", envelope.intent,
                     envelope.origin_agent)
        return {
            "ok": True,
            # The shape callers expect, filled in honestly: this turn went the
            # way ``mode="general"`` goes — answered as prose, no tools. The
            # real message is never routed, so no keyword in it (a report body
            # is full of them) can pick an agent.
            "route": RouteTarget.RAG.value,
            "category": "rag",
            "delegated_to": None,
            "delegate_reply": None,
            "intent": None,
            "targets": [],
            # Nothing was actioned, so the chat front door must not present this
            # as a completed errand. Callers that want the prose read "answer".
            "handled": False,
            "reply": None,
            "answer": answer,
        }

    def _converse(self, envelope: Envelope) -> dict:
        p = envelope.payload or {}
        text = str(p.get("text", p.get("message", "")))
        mode = str(p.get("mode", "auto"))

        # WHO IS SPEAKING decides whether this text may become a command, and it
        # is decided FIRST — before the pending-ask slot, before the resolver,
        # before the planning loop — because each of those is a side effect an
        # internal envelope must not have. See :meth:`_prose_only`.
        if (envelope.intent in PROSE_ONLY_INTENTS
                or envelope.origin_agent not in RESOLVING_ORIGINS):
            return self._prose_only(envelope, text, p)

        # A fresh clarification claims this turn: the user's message is the
        # ANSWER to the question the planning loop just asked, so re-plan the
        # ORIGINAL goal with that answer attached rather than treating the
        # answer ("a household member's profile, the wall tablet") as a brand-new request.
        answered_ask = None
        # The human-authored half of this turn, kept separate from the planner's
        # question all the way through. See ``_human_only`` below for why.
        request = text
        if text.strip() and self.has_pending_ask():
            if self._answers_the_ask(text):
                answered_ask = self._take_pending_ask()
            else:
                # A fresh errand supersedes the question. DROPPING the slot is
                # the point, not merely declining to spend it: a question left
                # armed still forces the following turn onto the agent path
                # (see ``_detect_route``) and still waits to swallow whatever
                # comes next. The person moved on; so does the slot.
                logger.info("a new request supersedes the pending "
                            "clarification; dropping it")
                self._pending_ask = None
            if answered_ask:
                request = f"{self._human_only(answered_ask)}\n{text}"
                text = (f"{answered_ask['goal']}\n"
                        f"(I asked: {answered_ask['question']}\n"
                        f"The user answered: {text})")

        # Routing and resolution read ``request``, never ``text``. On every
        # ordinary turn they are the same string; they differ only when this
        # turn answers a pending ask, and there the difference is the planner's
        # own question. Deciding WHICH INTENT and WHICH DEVICE from that is how
        #     turn 1  plan: human.ask "Should I turn on the porch light?"
        #     turn 2  user: "no"
        # ended with the porch light on: the planner failed to produce a plan on
        # turn 2, the loop degraded to single-step delegation, and this line
        # matched ``control_device`` — verb and device both — out of the
        # question rather than out of anything a person said. See
        # ``_human_only``.
        route = route_message(request, mode)
        category = get_route_category(request)

        # Resolved BEFORE the planning loop is considered, because the answer
        # decides whether planning is worth its latency (see _should_plan).
        # Cheap: a regex hit costs nothing, and the engine is consulted only on
        # a miss.
        resolution = self._resolve(request)

        # ── the bounded planning loop (kill-switch in planning_loop) ──
        # It claims the turn only when it produced a real run record. A missing
        # engine, an unparseable plan, or a plan with nothing the code actually
        # implements all return None and fall through to the single-step
        # delegation below — engine-less behaviour is exactly today's behaviour.
        if planning_loop_enabled() and (answered_ask is not None
                                        or self._should_plan(request, resolution)):
            # The planner's goal carries recent turns so follow-ups keep their
            # referent — EXCEPT when this turn answers a pending ask, where
            # ``text`` is already the combined goal+question+answer and the
            # history would inject the same turns twice.
            goal = text
            if answered_ask is None:
                preamble = self._context_preamble(
                    p.get("conversation_history"))
                if preamble:
                    goal = preamble + text
            # ``request`` rides along beside the goal: the loop plans from the
            # preamble-carrying goal but resolves device names — and seals its
            # egress scope — from what a PERSON typed. Otherwise a light named
            # two turns ago outscores the thermostat named just now, a URL the
            # assistant quoted back becomes a host this run may fetch, and (see
            # ``_human_only``) so does one the planner wrote into its own
            # question. It is ``request``, not ``text``, because on an answered
            # ask ``text`` has the planner's question folded into it.
            run = self._run_planning_loop(envelope, goal, request)
            # A run that ERRORED before executing anything is not an answer —
            # live case: the planner picked web_search for "what time is it",
            # egress refused it pre-dispatch, and the user heard a work log
            # about a failure instead of the time. Fall through: the resolver
            # and the legacy agent may still hold a real hand for this turn.
            # Denials stay terminal (an honest refusal must not get a second
            # bite) and clarifications stay — a question IS the answer.
            if run is not None \
                    and not run.get("executed") \
                    and run.get("stop_reason") not in (
                        "needs_clarification", "requires_confirmation",
                        "denied"):
                logger.info("planning run dry (%s, 0 executed); falling back",
                            run.get("stop_reason"))
                run = None
            if run is not None:
                if run.get("stop_reason") == "needs_clarification" \
                        and text.strip():
                    # Remember what we asked, so the next message can be read
                    # as the answer. ``note`` is the bare question; ``text`` is
                    # the (possibly already-combined) goal, which lets a second
                    # round of clarification chain instead of losing the goal.
                    # The ``text.strip()`` guard keeps a blank turn (which the
                    # answer guard above deliberately does NOT spend the slot
                    # on) from re-planning nothing and CLOBBERING the real
                    # pending goal with it.
                    # ``request`` is stashed beside ``goal`` so the human half
                    # accumulates on its own across chained clarifications: a
                    # second round must not re-admit the planner question the
                    # first round kept out (see ``_human_only``).
                    # ``rounds`` bounds the chain. Re-arming stamps a fresh
                    # ``ts``, so the TTL cannot end a planner that answers
                    # every answer with the same question — only a counter
                    # can. Past the cap the question is still DELIVERED (it is
                    # this turn's honest reply); it simply stops claiming the
                    # next turn, and the conversation starts clean.
                    rounds = int((answered_ask or {}).get("rounds", 0)) + 1
                    if rounds > self.PENDING_ASK_MAX_ROUNDS:
                        logger.info("clarification chain hit %d rounds; not "
                                    "re-arming the pending ask", rounds)
                        self._pending_ask = None
                    else:
                        self._pending_ask = {
                            "goal": text,
                            "request": request,
                            "question": str(run.get("note")
                                            or run.get("report") or ""),
                            "rounds": rounds,
                            "ts": time.monotonic(),
                        }
                return {
                    "ok": True,
                    "route": route.value if isinstance(route, RouteTarget)
                    else str(route),
                    "category": category,
                    "delegated_to": None,
                    "delegate_reply": None,
                    "intent": "plan",
                    "targets": [s.get("entity_id") for s in run.get("steps", [])
                                if s.get("entity_id")],
                    "handled": True,
                    "reply": run.get("report"),
                    "answer": None,
                    "plan": run,
                }

        # The route *category* is coarse (it never saw "take a snapshot from the
        # front door camera" as a home message). A matched specialist
        # rule therefore outranks it: the resolver picks the agent, and the
        # category only decides where an unmatched message goes.
        if resolution.intent is not None and resolution.reason != "no rule matched":
            target = "steward"
        else:
            target = _CATEGORY_TO_AGENT.get(category)

        delegated = None
        resolved_intent = None
        targets: list[str] = []
        reply_text = None
        handled = False
        if target and self.bus is not None and self.bus.get(target) is not None:
            # ``request``: this is the path the turn takes when the loop
            # degrades, and it resolves the ENTITY as well as the intent. A
            # device named only in the planner's question must not be actuated
            # here either — that is the exit the loop's own fix left open.
            outcome = self._delegate(envelope, request, category, target,
                                     resolution)
            delegated = outcome.get("delegate_reply")
            resolved_intent = outcome.get("intent")
            targets = outcome.get("targets") or []
            reply_text = outcome.get("reply")
            handled = bool(outcome.get("handled"))

        answer = None
        if self._engine is not None:
            try:
                answer = self._engine.run(text, p.get("conversation_history"))
            except Exception as exc:
                logger.warning("concierge engine failed: %s", exc)
                answer = None

        return {
            "ok": True,
            "route": route.value if isinstance(route, RouteTarget) else str(route),
            "category": category,
            "delegated_to": target if delegated is not None else None,
            "delegate_reply": delegated,
            "intent": resolved_intent,
            "targets": targets,
            # ``handled`` is the contract the chat front door reads: True means
            # the team produced an answer the user can be shown. False means
            # "not ours" — the caller must fall back to the legacy path.
            "handled": handled,
            "reply": reply_text,
            "answer": answer,
        }

    @staticmethod
    def _should_plan(text: str, resolution: Resolution) -> bool:
        """Whether this turn is worth a planning call.

        Planning costs a model call (~4-6s on this hardware) and answers in a
        step-by-step report. That is the right shape for "turn on the shed light
        and then photograph the shed", and the wrong shape for "how warm is it
        in the living room" — which the resolver answers in milliseconds and a
        person expects back as a sentence, not a work log.

        When the loop was gated on nothing but its own switch it ran FIRST, so
        every message paid for planning and a rule-matched question went from
        0.03s to 14s. So: plan when the request actually looks like more than
        one step, or when nothing could resolve it to a single capability
        (where a plan is the only remaining way to make progress).

        The cues are deliberately narrow — a bare "and" joins two nouns far more
        often than two actions ("temperature and humidity"), and mistaking that
        for a plan is how the fast path gets lost again.
        """
        # A plan is worth its latency only for genuinely multi-step
        # requests. An UNMATCHED single-capability read (pantry, vault,
        # prep, network, zone3, media sources, uploaded docs) is NOT a
        # planning job: the registry-bound legacy agent owns those tools
        # and answers them directly, while the planner's closed catalogue
        # lacks them and would fail the turn (often masking the miss as a
        # needless clarification). Plan ONLY on an explicit multi-step cue;
        # everything else falls to the resolver + legacy agent, which
        # returns handled=False on no rule so the owning tool runs.
        if _MULTI_STEP_RE.search(text or ""):
            return True
        # ...and on a second cue the explicit-connective list cannot see: two
        # segments that resolve to DIFFERENT intents where one of them acts.
        # This does NOT weaken the rule above -- a segment that fails to
        # resolve is skipped, so an unmatched read still never reaches the
        # planner and the registry-bound legacy agent keeps those turns.
        if ConciergeAgent._spans_multiple_actions(text):
            return True
        # An UNMATCHED turn plans only when it is an information goal. This
        # keeps the registry-first rule intact -- pantry, vault, prep,
        # network and zone-3 reads still go straight to the registry-bound
        # legacy agent -- while letting the turns the planner can now actually
        # compose reach it. See _is_information_goal.
        if resolution.intent is None or resolution.reason == "no rule matched":
            return ConciergeAgent._is_information_goal(text)
        return False

    @staticmethod
    def _is_information_goal(text: str) -> bool:
        """Whether an unmatched turn is one the planner could now compose.

        Keyed to the SAME policy the Toolsmith's registry bridge enforces
        (BRIDGE_AGENTS, BRIDGE_DENIED_CAPS, read-only), because the two answer
        the same question from opposite ends: the bridge decides what a plan
        step may reach, and this decides whether planning is worth attempting.
        Planning a goal whose tools the bridge would refuse buys a stall.

        "add a NASA news feed" plans (news.headlines: majordomo, read);
        "what's in the pantry" does not (pantry.shopping: steward); "show me
        the network status" does not (security.network: sentinel) -- even
        though the resolver returns "no rule matched" for all three.

        Never raises: an unavailable registry means "not an information goal".
        """
        try:
            from puffpai import capability_registry as registry
            from .toolsmith import BRIDGE_AGENTS, BRIDGE_DENIED_CAPS
            cap = registry.find(text or "")
            if cap is None:
                return False
            return (cap.agent in BRIDGE_AGENTS
                    and cap.access == "read"
                    and cap.id not in BRIDGE_DENIED_CAPS)
        except Exception as exc:
            logger.debug("information-goal check unavailable: %s", exc)
            return False

    @staticmethod
    def _spans_multiple_actions(text: str) -> bool:
        """True when a request names two different intents and one of them acts.

        The connective cues in :data:`_MULTI_STEP_RE` only fire on an explicit
        "then"/"also"/";", which misses this method's own worked example --
        "turn on the shed light and photograph the shed". A bare "and" is not a
        cue, so that request resolved to ``control_device`` alone and the
        snapshot silently never ran.

        Widening the cue list is what breaks "temperature and humidity", so
        instead split on the broad connectives and re-resolve each segment.
        ``resolve_intent`` is regex-only, so this costs microseconds and never a
        model call. Segments count only when they resolve to DIFFERENT intents,
        and the turn only plans when at least one of them changes state -- two
        reads stay on the fast path, and an unresolved segment is skipped so the
        registry-bound legacy agent keeps the turns it owns.
        """
        segments = [seg for seg in _SEGMENT_SPLIT_RE.split(text or "")
                    if seg and seg.strip()]
        if len(segments) < 2:
            return False
        intents: set[str] = set()
        acts = False
        for segment in segments:
            found = resolve_intent(segment)
            if found.intent is None or found.reason == "no rule matched":
                continue
            intents.add(found.intent)
            acts = acts or found.state_changing
        return acts and len(intents) >= 2

    def _run_planning_loop(self, envelope: Envelope, text: str,
                           request: str | None = None) -> dict | None:
        """Attempt a planned run. Never raises — a failure degrades, it does not
        escalate: the fallback path is strictly less capable, never less safe.

        *text* is the planner's goal (conversation preamble and all); *request*
        is the bare message. The loop resolves device names and seals its egress
        scope from *request*, so neither an old turn's device nor a host the
        assistant quoted back can ride in on the preamble.
        """
        if self.bus is None:
            return None
        try:
            return self.planning_loop().run(envelope, text, request)
        except Exception as exc:
            logger.warning("planning loop failed, degrading to single-step: %s",
                           exc)
            return None

    def _delegate(self, envelope: Envelope, text: str, category: str,
                  target: str, resolution: Resolution | None = None) -> dict:
        """Resolve an intent (+ target entity) and send it to the owning agent.

        Returns ``{intent, targets, delegate_reply, reply, handled}``. Never
        raises: budget exhaustion or a missing bus degrades to ``handled=False``
        so the caller can fall back.
        """
        if target == "steward":
            return self._delegate_to_steward(
                envelope, text, category,
                resolution if resolution is not None else self._resolve(text))

        if target == "archivist":
            intent = "retrieve"
            payload: dict = {"query": text,
                             "profile": (envelope.payload or {}).get("profile")}
        elif target == "majordomo":
            intent = "list_queue"
            payload = {}
        elif target == "sentinel":
            intent = "security_status"
            payload = {}
        else:
            return {"intent": None, "targets": [], "handled": False,
                    "delegate_reply": {"ok": False,
                                       "error": f"no delegation mapping for {category}"}}

        reply = self._send_sub(envelope, target, intent, payload,
                               state_changing=False)
        return {"intent": intent, "targets": [], "delegate_reply": reply,
                "reply": None, "handled": False}

    # ── steward delegation: intent + entity resolution ──────────────
    def _delegate_to_steward(self, envelope: Envelope, text: str,
                             category: str, resolution: Resolution) -> dict:
        # No rule matched. Only a message the router already called "home" gets
        # the safe ``list_devices`` default — otherwise a calendar or recipe
        # question would be answered with a list of light switches.
        # Prefix, not equality: an engine-refined resolution reports
        # "no rule matched (engine refined)" so the guard below still applies
        # to it. See _engine_refine_intent.
        unmatched = str(resolution.reason or "").startswith("no rule matched")
        if resolution.intent is None or (unmatched and category != "home"):
            logger.debug("concierge: leaving %r to the legacy path (%s)",
                         text[:60], resolution.reason or "unmatched")
            return {"intent": None, "targets": [], "delegate_reply": None,
                    "reply": None, "handled": False}

        payload = dict(resolution.payload)
        entity_id = None
        if resolution.needs_entity:
            entity_id, problem = self._resolve_entity(
                envelope, text, resolution.domains)
            if entity_id is None:
                # ``problem`` is None only when the device list itself was
                # unreachable — then the team genuinely cannot serve the turn.
                return {"intent": resolution.intent, "targets": [],
                        "delegate_reply": None, "reply": problem,
                        "handled": problem is not None}
            payload["entity_id"] = entity_id

        targets: list[str] = []
        if resolution.state_changing:
            # Sentinel's consent gate only engages when ``targets`` is present,
            # and ``bus.send`` only asks Sentinel when state_changing is True.
            # Both must be set together or the veto silently does nothing.
            if entity_id:
                targets = [entity_id]
                payload["targets"] = targets
            payload.setdefault("task_text", text[:200])

        reply = self._send_sub(envelope, "steward", resolution.intent, payload,
                               state_changing=resolution.state_changing)
        prose = self._render(resolution.intent, entity_id, reply)
        return {"intent": resolution.intent, "targets": targets,
                "delegate_reply": reply, "reply": prose,
                "handled": prose is not None}

    def _resolve(self, text: str) -> Resolution:
        """Deterministic resolution, optionally refined by the engine."""
        resolution = resolve_intent(text)
        if resolution.reason == "no rule matched":
            refined = self._engine_refine_intent(text)
            if refined is not None:
                return refined
        return resolution

    def _engine_refine_intent(self, text: str) -> Resolution | None:
        """Optional LLM narrowing of the fallback. Never required.

        Only consulted when no deterministic rule matched, and only when the
        injected engine exposes a ``classify_intent(text, choices)`` method. Any
        answer outside :data:`RESOLVABLE_INTENTS` is discarded — the model may
        pick from the menu, it may not invent a hand the Steward does not have.
        """
        classify = getattr(self._engine, "classify_intent", None)
        if not callable(classify):
            return None
        try:
            choice = classify(text, sorted(RESOLVABLE_INTENTS))
        except Exception as exc:
            logger.debug("concierge intent refinement failed: %s", exc)
            return None
        if not isinstance(choice, str) or choice not in RESOLVABLE_INTENTS:
            return None
        needs_entity = choice in ("device_status", "camera_snapshot",
                                  "control_device", "activate_scene",
                                  "set_climate", "siren")
        domains = {"camera_snapshot": ("camera",), "activate_scene": ("scene",),
                   "set_climate": ("climate",), "siren": ("siren",)}.get(choice, ())
        # The reason KEEPS the "no rule matched" prefix. A refinement is still
        # a guess about a turn no deterministic rule claimed, and
        # _delegate_to_steward's category guard exists precisely to stop such a
        # guess answering a non-home question with a home tool. Returning a
        # fresh reason here silently disarmed that guard, which is how
        # "set an alarm for 7am" reached set_climate, "did I take my
        # medication today" reached media_status, "do we owe any money on
        # bills" reached device_status, and "do we have any events or
        # reminders coming up" reached list_devices and dumped 111 entities.
        return Resolution(choice, domains, {}, needs_entity=needs_entity,
                          reason="no rule matched (engine refined)")

    def _resolve_entity(self, envelope: Envelope, text: str,
                        domains: tuple[str, ...],
                        devices: list[dict] | None = None
                        ) -> tuple[str | None, str | None]:
        """Find the entity the user meant.

        Returns ``(entity_id, None)`` on success, ``(None, message)`` when the
        match was ambiguous or absent (the message names close candidates), and
        ``(None, None)`` when the device list could not be fetched at all.

        The device list is a read-only Steward call charged against the parent
        envelope's budget; this house's entity ids are never hardcoded. A caller
        resolving several targets in one run (the planning loop) may pass an
        already-fetched *devices* list so the house is asked once, not per step.
        """
        # A literal entity id in the text is a strong signal, but only a SHAPE
        # signal: ``domain.object_id`` is also the shape of a hostname, so
        # "read https://nope.example/x and then turn on the porch light" would
        # otherwise hand back ``nope.example`` as the device target and let it
        # travel as far as the consent gate. Shape is not membership. The house's own
        # device list is the only real allow-list — a name is a device because
        # this house HAS it, not because it parses — so a literal has to appear
        # in it. That also covers ids the model invented rather than read.
        #
        # When the list cannot be fetched there is nothing to check against, so
        # this fails closed rather than falling back to trusting the text.
        literal = _ENTITY_IN_TEXT_RE.search((text or "").lower())
        candidate = literal.group(1) if literal is not None else None
        if (candidate is not None and is_valid_entity_id(candidate)
                and (not domains or candidate.split(".")[0] in domains)):
            if devices is None:
                devices = self._ask_devices(envelope)
            if devices is None:
                return None, None
            if any(dev.get("entity_id") == candidate for dev in devices):
                return candidate, None
            # Not this house's: fall through and score it by name like any
            # other phrasing, so the porch light in that sentence still wins.

        if devices is None:
            devices = self._ask_devices(envelope)
        if devices is None:
            return None, None

        text_tokens = _tokens(text)
        # A caller that names no domains still gets a domain gate when the
        # REQUEST names a device type — same rule for the fast path and the
        # planning loop, since both resolve here.
        if not domains:
            domains = _implied_domains(text_tokens)
        scored: list[tuple[tuple[int, float], str, str]] = []
        for dev in devices:
            eid = dev.get("entity_id")
            if not isinstance(eid, str) or "." not in eid:
                continue
            if domains and eid.split(".")[0] not in domains:
                continue
            name = str(dev.get("name") or "")
            name_tokens = _tokens(name) | _tokens(eid.split(".", 1)[1])
            score = _score_device(name_tokens, text_tokens)
            if score[0] > 0:
                scored.append((score, eid, name or eid))

        what = f"{domains[0]} " if len(domains) == 1 else ""
        if not scored:
            # Climate named by TYPE but no room ("set the thermostat/temp
            # to 73") matched no entity by tokens. With ONE climate entity
            # that is the one they mean; with several, ask which ROOM
            # instead of a confusing "could not find".
            if domains == ("climate",):
                climates = [d for d in devices
                            if isinstance(d.get("entity_id"), str)
                            and d["entity_id"].split(".")[0] == "climate"]
                if len(climates) == 1:
                    return climates[0]["entity_id"], None
                if len(climates) > 1:
                    names = ", ".join(
                        (d.get("name") or d.get("entity_id", "").split(".")[-1]
                         .replace("_", " ").title())
                        for d in climates)
                    return None, ("Which thermostat do you mean: " + names
                                  + "? Name the room, like the living room "
                                  "or the bedroom.")
            # When the request named a device TYPE, listing what the house
            # actually has in those domains beats a generic brush-off:
            # "add my second reolink camera" should come back with the two
            # cameras' real names, not "ask me to list devices".
            if domains:
                have = [str(d.get("name") or d.get("entity_id") or "")[:48]
                        for d in devices
                        if isinstance(d.get("entity_id"), str)
                        and d["entity_id"].split(".")[0] in domains][:5]
                if have:
                    return None, (f"I couldn't find a {what}device matching "
                                  f"that. Of that kind I can see: "
                                  f"{', '.join(have)}.")
            return None, (f"I couldn't find a {what}device matching that. "
                          "Ask me to list devices and I'll show you what I can see.")

        scored.sort(key=lambda row: row[0], reverse=True)
        best = scored[0]
        if best[0][1] < 0.5:
            names = ", ".join(row[2] for row in scored[:3])
            return None, (f"I'm not sure which {what}device you mean. "
                          f"Closest matches: {names}.")
        if len(scored) > 1 and scored[1][0] == best[0]:
            names = ", ".join(row[2] for row in scored
                              if row[0] == best[0])[:200]
            return None, f"That matches more than one device — did you mean {names}?"
        return best[1], None

    def _ask_devices(self, envelope: Envelope) -> list[dict] | None:
        """Budgeted, read-only device list from the Steward. None on failure."""
        reply = self._ask_read_only(envelope, "steward", "list_devices",
                                    {"controllable_only": False})
        if not isinstance(reply, dict) or not reply.get("ok"):
            return None
        devices = reply.get("devices")
        return devices if isinstance(devices, list) else None

    #: Default slice for a delegated sub-request when the caller names none.
    SUB_BUDGET_ACTIONS = 3
    SUB_BUDGET_SECONDS = 15.0

    def _sub_budget(self, envelope: Envelope,
                    requested: Budget | None) -> Budget:
        """A sub-budget that is provably no larger than what the parent holds.

        Delegated budgets are "only ever smaller" by code, not by claim: a
        caller-supplied ``max_actions`` of ``9999`` is clamped, not honoured.

        Three properties, all deliberate:

        * **Always a fresh object.** The caller's :class:`Budget` is read, never
          written: flipping ``allow_egress`` on the object it was handed
          would surprise a caller that reuses it.
        * **Fail closed on a spent parent.** A parent with nothing left yields a
          zero-action budget, not a floor of one; the sub-request then aborts on
          its first :meth:`Budget.consume` rather than getting a free action.
        * **Egress needs both halves.** A delegated budget carries
          ``allow_egress`` only when the caller explicitly asked for it *and*
          the parent envelope already held it. Never minted (a caller cannot
          conjure network reach the request did not arrive with) and never
          inherited silently (a step that had no reason to ask does not get it
          just because the run it belongs to could). Returning ``False``
          unconditionally would be safe but would mean a planned
          ``toolsmith.web_fetch`` could never run at all.
        """
        actions = (self.SUB_BUDGET_ACTIONS if requested is None
                   else int(requested.max_actions))
        seconds = (self.SUB_BUDGET_SECONDS if requested is None
                   else float(requested.max_seconds))
        parent = getattr(envelope, "budget", None)
        egress = bool(requested is not None and requested.allow_egress
                      and isinstance(parent, Budget) and parent.allow_egress)
        if isinstance(parent, Budget):
            actions = min(actions, parent.actions_remaining)
            seconds = min(seconds, parent.max_seconds - parent.elapsed)
        return Budget(max_actions=max(0, actions), max_seconds=max(0.0, seconds),
                      allow_egress=egress)

    def _send_sub(self, envelope: Envelope, target: str, intent: str,
                  payload: dict, *, state_changing: bool,
                  budget: Budget | None = None) -> dict:
        """Mint and send a delegated envelope. ``origin_agent`` stays this agent.

        The Concierge must never stamp a human origin: the Steward's
        always-confirm gate is exactly that check, so anything in the
        safety-critical class asked for in chat can only come back as a proposal.
        """
        reply, _spent = self._send_sub_metered(
            envelope, target, intent, payload, state_changing=state_changing,
            budget=budget)
        return reply

    def _send_sub_metered(self, envelope: Envelope, target: str, intent: str,
                          payload: dict, *, state_changing: bool,
                          budget: Budget | None = None) -> tuple[dict, int]:
        """:meth:`_send_sub`, plus how many actions the sub-request really spent.

        The planning loop needs the real number: charging its run budget one
        notional action per step while the step spent three is how a 12-action
        ceiling paid for 18 actions. The count is read off the budget this method
        minted, not reported by the callee, so it cannot be understated.
        """
        sub_budget = self._sub_budget(envelope, budget)
        sub = Envelope(
            intent=intent,
            payload=payload,
            requester=envelope.requester,
            origin_agent=self.name,
            budget=sub_budget,
            state_changing=state_changing,
        )
        try:
            reply = self.ask(target, sub)
        except Exception as exc:
            logger.debug("delegation failed (%s.%s): %s", target, intent, exc)
            reply = {"ok": False, "error": str(exc)}
        return reply, sub_budget.actions_used

    # ── rendering the specialist's reply as prose ───────────────────
    def _render(self, intent: str, entity_id: str | None, reply) -> str | None:
        """Turn a Steward reply into something the user can read.

        ``None`` means "not usable" — the chat front door then falls back to the
        legacy path. A Sentinel *denial* is usable: it is the team answering,
        and laundering it into the legacy agent would defeat the veto.
        """
        if not isinstance(reply, dict):
            return None
        who = entity_id or "that device"

        if reply.get("denied"):
            return (f"I can't do that yet — {reply.get('by', 'Sentinel')} blocked it: "
                    f"{reply.get('reason') or 'not approved'}. Approve {who} in "
                    "the device consent settings and ask me again.")
        if reply.get("unavailable"):
            return f"{who} is offline right now (state: {reply.get('state')!r})."
        if not reply.get("ok"):
            return None

        if intent == "siren":
            if reply.get("executed"):
                return f"Siren {reply.get('service', 'triggered')} on {who}."
            proposal = reply.get("proposal") or {}
            return (f"I can run {proposal.get('service', 'the siren')} on "
                    f"{proposal.get('entity_id', who)}, but a siren always needs your "
                    "explicit confirmation — I won't set it off on my own.")
        if intent == "camera_snapshot":
            return (f"Captured a snapshot from {who} "
                    f"({reply.get('bytes', 0)} bytes, {reply.get('content_type')}).")
        if intent == "device_status":
            described = _describe_state(reply.get("state"))
            reading = _describe_reading(reply.get("state"))
            if described and reading:
                return f"{who} is {described} — {reading}."
            if reading:
                return f"{who} is {reading}."
            return f"{who} is {described}." if described else None
        if intent == "control_device":
            action = (reply.get("result") or {}).get("action") \
                if isinstance(reply.get("result"), dict) else None
            return f"Done — {who} {str(action or 'updated').replace('_', ' ')}."
        if intent == "activate_scene":
            return f"Activated {who}."
        if intent == "set_climate":
            bits = []
            if reply.get("hvac_mode"):
                bits.append(f"mode {reply['hvac_mode']}")
            if reply.get("temperature") is not None:
                bits.append(f"{reply['temperature']:g}°")
            return f"Set {who} to {' and '.join(bits)}." if bits else f"Updated {who}."
        if intent == "notify_user":
            return f"Sent you a notification: {reply.get('message', '')}"
        if intent == "announce":
            return f"Announced on {reply.get('entity_id', 'the house speakers')}."
        if intent == "list_add":
            which = reply.get("list") or "shopping"
            if which != "shopping":
                return f"Added {reply.get('item')} to the {which} list."
            # EVIDENCE, not intent. The Steward read the store back after
            # writing (puffpai.agents.shopping_writes.add_verified), and the
            # sentence is composed from what the list holds now. A template
            # saying "Added oat milk to the shopping list" for a write that
            # landed in a store nothing reads is a confirmation with no
            # read-back behind it: a guess wearing a receipt.
            from puffpai.agents import shopping_writes
            return shopping_writes.reply_for(reply)
        if intent in ("zigbee_pair", "zigbee_pair_status"):
            # Both replies already carry a sentence written where the facts are
            # (puffpai.zigbee_pairing), including the "this needs your approval"
            # wording. Restating it here would let the two drift.
            return reply.get("message") or None
        if intent == "media_status":
            status = reply.get("status")
            return f"Media server status: {status}" if status else None
        if intent == "media_search":
            count = reply.get('count', 0)
            titles = [t for t in (reply.get('titles') or []) if t]
            if not count or not titles:
                return "I didn't find any matching titles in your library."
            shown = titles[:10]
            more = f" (and {count - len(shown)} more)" if count > len(shown) else ""
            return "In your library: " + "; ".join(shown) + more + "."
        if intent == "list_devices":
            devices = reply.get("devices") or []
            if not devices:
                return "I can't see any devices in the house right now."
            names = ", ".join(str(d.get("name") or d.get("entity_id"))
                              for d in devices[:12])
            more = f" (+{len(devices) - 12} more)" if len(devices) > 12 else ""
            return f"I can see {len(devices)} device(s): {names}{more}."
        return None

    # ── event classification ─────────────────────────────────────────
    def _classify_event(self, envelope: Envelope) -> dict:
        """Turn a pushed observation into a decision dict.

        Deliberately **deterministic**: the classification must work with no LLM
        engine attached, because event inflow runs on background threads that
        may fire while the model is cold or unavailable. The engine, when
        present, only adds prose — never the severity.

        Never raises and never exceeds the envelope budget: any failure degrades
        to an informational, logged decision.
        """
        p = envelope.payload or {}
        source = str(p.get("source", "unknown"))
        event_type = str(p.get("event_type", ""))
        data = p.get("data") or {}
        if not isinstance(data, dict):
            data = {}

        try:
            if event_type in ("camera_person", "camera_motion"):
                decision = self._decide_camera(envelope, event_type, data)
            elif event_type == "network_new_device":
                decision = self._decide_new_device(envelope, data)
            elif event_type == "weather_alert":
                decision = self._decide_weather(data)
            elif event_type == "service_degraded":
                decision = self._decide_service_degraded(data)
            elif event_type == "self_check":
                decision = self._decide_self_check(data)
            else:
                decision = {
                    "severity": "info",
                    "summary": f"unrecognized event {event_type!r} from {source}",
                    "suggested_action": "logged",
                }
        except Exception as exc:  # budget, bus, or a malformed payload
            logger.warning("event classification failed (%s/%s): %s",
                           source, event_type, exc)
            decision = {
                "severity": "info",
                "summary": f"event {event_type!r} from {source} could not be classified",
                "suggested_action": "logged",
                "error": str(exc),
            }

        return {"ok": True, "source": source, "event_type": event_type,
                "decision": decision}

    def _decide_camera(self, envelope: Envelope, event_type: str,
                       data: dict) -> dict:
        """Camera person/motion: ask the Steward for the entity's live state.

        Policy: a *person* at night is worth raising a hand
        about; motion, or a person in daylight, is informational. Nothing here
        acts: ``suggested_action`` stays ``logged``; the team has no
        announce/notify hands for it.
        """
        raw_entity_id = data.get("entity_id")
        # An event is attacker-reachable (any producer that can POST /agents/event).
        # A malformed entity_id must never become an authenticated HA lookup, so
        # it is dropped here as well as in the bridge and the route.
        if raw_entity_id is not None and not is_valid_entity_id(raw_entity_id):
            logger.warning("event %s carried a malformed entity_id; ignoring it",
                           event_type)
            return {
                "severity": "info",
                "summary": (f"{'person' if event_type == 'camera_person' else 'motion'}"
                            " event ignored: invalid entity id"),
                "suggested_action": "logged",
                "entity_id": None,
                "entity_state": None,
                "night": is_night(),
                "invalid_entity_id": True,
            }

        entity_id = str(raw_entity_id or "")
        state = self._ask_state(envelope, entity_id)
        night = is_night()
        severity = "attention" if (event_type == "camera_person" and night) else "info"

        label = "person" if event_type == "camera_person" else "motion"
        where = str(data.get("friendly_name") or entity_id or "camera")[:64]
        summary = f"{label} detected at {where}"
        if night:
            summary += " during the night window (22:00-06:00)"
        described = _describe_state(state)
        if described:
            summary += f"; current state {described}"

        return {
            "severity": severity,
            "summary": summary,
            "suggested_action": "logged",
            "entity_id": entity_id or None,
            "entity_state": state,
            "night": night,
        }

    def _decide_new_device(self, envelope: Envelope, data: dict) -> dict:
        """A device we have never seen joined the network — always attention."""
        status = self._ask_sentinel_status(envelope)
        who = str(data.get("name") or data.get("mac") or data.get("ip")
                  or "unknown device")[:64]
        summary = f"new device on the network: {who}"
        pending = (status or {}).get("pending_alerts")
        if pending is not None:
            summary += f"; sentinel has {pending} pending alert(s)"
        return {
            "severity": "attention",
            "summary": summary,
            "suggested_action": "logged",
            "device": {k: data.get(k) for k in ("mac", "ip", "name")
                       if data.get(k)},
            "sentinel_status": status,
        }

    @staticmethod
    def _decide_weather(data: dict) -> dict:
        """Weather alert: any live NWS alert is attention-worthy."""
        headlines = data.get("headlines") or []
        if isinstance(headlines, str):
            headlines = [headlines]
        headlines = [str(h) for h in list(headlines)[:5] if h]
        try:
            count = int(data.get("count", len(headlines)))
        except (TypeError, ValueError):
            count = len(headlines)

        if count <= 0 and not headlines:
            return {"severity": "info", "summary": "no active weather alerts",
                    "suggested_action": "logged", "count": 0, "headlines": []}

        summary = f"{count} active weather alert(s)"
        if headlines:
            summary += ": " + "; ".join(headlines)
        elif data.get("summary"):
            summary += ": " + str(data["summary"])
        return {
            "severity": "attention",
            "summary": summary,
            "suggested_action": "logged",
            "count": count,
            "headlines": headlines,
        }

    @staticmethod
    def _decide_service_degraded(data: dict) -> dict:
        """A health check found something down.

        Detect → classify → **suggest**. This method never restarts anything and
        never asks anyone to: ``suggested_action`` is a recommendation that a
        human (or, later, a gated Majordomo/Caretaker path) may act on. The
        Caretaker keeps its own allowlist, its self-restart refusal and its
        rate-limit ledger, and none of that is reachable from here.

        Severity: ``attention`` when one of PuffPAI's own services is down,
        ``info`` when only a peripheral (Home Assistant) is — the house has
        offline hardware routinely and that is not a PuffPAI fault.

        Deterministic, engine-less, no bus lookups (an ask would spend the event
        envelope's budget to re-learn what the payload already says).
        """
        raw = data.get("down_services")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            raw = []
        # The producer is any process that can POST /agents/event, so names are
        # length-capped before they reach a persisted summary.
        names = [str(n)[:64] for n in list(raw)[:12] if n]

        try:
            down_count = int(data.get("down_count", len(names)))
        except (TypeError, ValueError):
            down_count = len(names)
        overall = str(data.get("overall") or "")[:32]

        base = {
            "down_count": max(down_count, 0),
            "down_services": names,
            "overall": overall or ("healthy" if down_count <= 0 else "degraded"),
            # Said plainly, in the payload itself: nothing was done to the box.
            "suggestion_only": True,
            "action_taken": None,
            "note": ("suggested_action is a recommendation only; the Concierge "
                     "does not restart services — the Caretaker's gated, "
                     "rate-limited path does, if a human or a gated caller "
                     "asks it to"),
        }

        if not names:
            if down_count <= 0:
                # Recovery (or a healthy probe someone posted anyway): forget the
                # remembered fault so the next degradation reports immediately.
                reset_service_degraded_dedup()
                return {**base, "severity": "info", "suggested_action": "logged",
                        "summary": "all monitored services are up"}
            # Something is down but the payload never said what.
            return {**base, "severity": "info", "suggested_action": "needs_human",
                    "summary": (f"{down_count} service(s) reported down but none "
                                "were named")}

        keys = [service_key(n) for n in names]
        core = [k for k in keys if k in CORE_SERVICES]
        unknown = [names[i] for i, k in enumerate(keys)
                   if k not in CORE_SERVICES and k not in PERIPHERAL_SERVICES]

        # Peripherals are excluded from the action decision: there is nothing on
        # this appliance to restart that would bring the neighbour's box back, so
        # letting Home Assistant sit in the set would drag a perfectly fixable
        # dashboard fault into "needs_human".
        actionable = sorted({k for k in keys if k in CORE_SERVICES})
        restartable = sorted(
            k for k in actionable
            if k in RESTARTABLE_UNITS and k != DEFAULT_SELF_UNIT)

        severity = "attention" if core else "info"
        summary = (f"{len(names)} service(s) down: " + ", ".join(names))

        if not actionable and not unknown:
            suggested = "logged"          # peripherals only — record and move on
        elif not unknown and actionable and len(restartable) == len(actionable):
            suggested = "restart_service"
        else:
            # Core service with no safe automated remedy (puffpai-api is a
            # self-restart the Caretaker refuses; ollama is deliberately not
            # allowlisted), or a service this table has never heard of.
            suggested = "needs_human"

        decision = {
            **base,
            "severity": severity,
            "summary": summary,
            "suggested_action": suggested,
            "core_down": sorted(set(core)),
            "peripheral_down": sorted({k for k in keys
                                       if k in PERIPHERAL_SERVICES}),
            "unknown_down": unknown,
            "restartable_units": restartable,
        }
        if suggested == "restart_service":
            decision["unit"] = restartable[0]

        # DEDUP (layer 2 of 2, and the authoritative one — see the Node-RED
        # `scheduled health check -> agent event` node for layer 1). Keyed on the
        # exact down-set, so an escalation (another service failing, or a
        # different one) is always reported; only an unchanged fault is folded.
        fingerprint = frozenset(keys[i] or f"?{names[i]}" for i in range(len(names)))
        duplicate = _note_and_check_duplicate(fingerprint)

        if duplicate:
            decision["severity"] = "info"
            decision["suggested_action"] = "logged"
            decision["escalated_action"] = suggested
            decision["duplicate"] = True
            decision["summary"] = (
                f"{summary} (unchanged in the last "
                f"{int(SERVICE_DEGRADED_DEDUP_SECONDS // 60)} minutes; "
                "already reported)")
        else:
            decision["duplicate"] = False
        return decision

    @staticmethod
    def _decide_self_check(data: dict) -> dict:
        """Findings from :func:`puffpai.self_check.run_self_check`.

        Classify → **suggest**. Like ``service_degraded`` this method acts on
        nothing: the self-check module owns the one narrow repair it is allowed
        to make (creating a missing state directory under an already-writable
        root), and it only makes it when its own caller asked for repairs. The
        Concierge cannot request one from here, and nothing in this path
        restarts a service, writes to the house, or touches a credential.

        Severity: ``attention`` only when a check *failed* and that check is a
        fault in the appliance. Everything in
        :data:`SELF_CHECK_INFORMATIONAL` — devices offline, vault awaiting its
        PIN — stays ``info`` however loud it looks.

        Deterministic, engine-less, no bus lookups: the payload already carries
        the findings, so an ask would spend the envelope's budget re-learning
        them.
        """
        raw = data.get("checks")
        if raw is None and isinstance(data.get("self_check"), dict):
            raw = data["self_check"].get("checks")
        if not isinstance(raw, (list, tuple)):
            raw = []

        # The producer is any process that can POST /agents/event, so every
        # field is length-capped before it reaches a persisted summary.
        checks = []
        for item in list(raw)[:64]:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").strip().lower()
            checks.append({
                "id": str(item.get("id") or "unknown")[:64],
                "status": status if status in ("ok", "warn", "fail") else "warn",
                "title": str(item.get("title") or "")[:80],
                "detail": str(item.get("detail") or "")[:240],
                "suggestion": str(item.get("suggestion") or "")[:240],
                "repaired": bool(item.get("repaired")),
            })

        notable = [c for c in checks if c["status"] in ("warn", "fail")]
        failures = [c for c in notable
                    if c["status"] == "fail"
                    and c["id"] not in SELF_CHECK_INFORMATIONAL]
        repaired = [c["id"] for c in checks if c["repaired"]]

        base = {
            "checks_run": len(checks),
            "failed": [c["id"] for c in checks if c["status"] == "fail"],
            "warned": [c["id"] for c in checks if c["status"] == "warn"],
            "repaired": repaired,
            "findings": [{"id": c["id"], "status": c["status"],
                          "detail": c["detail"], "suggestion": c["suggestion"]}
                         for c in notable],
            # Said plainly, in the payload itself.
            "suggestion_only": True,
            "action_taken": None,
            "note": ("suggested_action is a recommendation only; the Concierge "
                     "does not repair state, restart services, touch "
                     "credentials or write to the house"),
        }

        if not checks:
            _forget_duplicates("self_check:")
            return {**base, "severity": "info", "suggested_action": "logged",
                    "summary": "self-check reported no findings"}

        if not notable:
            # Everything clean: forget the remembered findings so the next real
            # fault reports immediately instead of being folded as a repeat.
            _forget_duplicates("self_check:")
            summary = f"self-check passed all {len(checks)} checks"
            if repaired:
                summary += f" (repaired: {', '.join(repaired[:6])})"
            return {**base, "severity": "info", "suggested_action": "logged",
                    "summary": summary}

        severity = "attention" if failures else "info"
        suggested = "needs_human"
        headline = ", ".join(f"{c['id']} [{c['status']}]" for c in notable[:6])
        summary = f"self-check raised {len(notable)} finding(s): {headline}"
        if repaired:
            summary += f"; already repaired: {', '.join(repaired[:6])}"

        decision = {**base, "severity": severity, "suggested_action": suggested,
                    "informational_only": not failures}

        # DEDUP: the same mechanism ``service_degraded`` established, keyed on
        # the exact set of findings and namespaced so the two producers share
        # the table without colliding. A new or changed finding always reports;
        # only an unchanged picture is folded.
        fingerprint = frozenset(f"self_check:{c['id']}:{c['status']}"
                                for c in notable)
        if _note_and_check_duplicate(fingerprint):
            decision["severity"] = "info"
            decision["suggested_action"] = "logged"
            decision["escalated_action"] = suggested
            decision["duplicate"] = True
            decision["summary"] = (
                f"{summary} (unchanged in the last "
                f"{int(SERVICE_DEGRADED_DEDUP_SECONDS // 60)} minutes; "
                "already reported)")
        else:
            decision["duplicate"] = False
            decision["summary"] = summary
        return decision

    # ── budgeted lookups through the bus ────────────────────────────
    def _ask_state(self, envelope: Envelope, entity_id: str):
        """Ask the Steward for an entity's state; None when unavailable.

        Re-validates the id: this is the last hop before an authenticated HA
        read, and no caller gets to skip the check.
        """
        if (not is_valid_entity_id(entity_id) or self.bus is None
                or self.bus.get("steward") is None):
            return None
        reply = self._ask_read_only(envelope, "steward", "device_status",
                                    {"entity_id": entity_id})
        return (reply or {}).get("state")

    def _ask_sentinel_status(self, envelope: Envelope):
        """Ask Sentinel for its monitoring status; None when unavailable."""
        if self.bus is None or self.bus.get("sentinel") is None:
            return None
        reply = self._ask_read_only(envelope, "sentinel", "security_status", {})
        return (reply or {}).get("status")

    def _ask_read_only(self, envelope: Envelope, target: str, intent: str,
                       payload: dict):
        """Charge one action to the event envelope and make a read-only ask.

        Failures (including :class:`BudgetExceeded`) are swallowed: an event
        decision must still be produced when a lookup is unavailable.
        """
        try:
            envelope.budget.consume(1)
        except Exception as exc:
            logger.debug("event lookup skipped (%s): %s", target, exc)
            return None
        sub = Envelope(
            intent=intent,
            payload=payload,
            requester=envelope.requester,
            origin_agent=self.name,
            budget=Budget(max_actions=1, max_seconds=10.0, allow_egress=False),
            state_changing=False,
        )
        try:
            return self.ask(target, sub)
        except Exception as exc:
            logger.debug("event lookup failed (%s.%s): %s", target, intent, exc)
            return None

    def health(self) -> dict:
        base = super().health()
        base["engine_attached"] = self._engine is not None
        base["holds_secrets"] = False
        # Name the brain, without touching the network: /agents/status is polled
        # by the dashboard, so probing the model runtime here would put a
        # remote call (and its timeout) on a status endpoint. "Which model is
        # attached" is the operator's question; "is Ollama up" is answered by
        # /system/status, which already owns that probe.
        if self._engine is not None:
            models = {
                key: getattr(self._engine, attr, None)
                for key, attr in (("classify", "_classify_model"),
                                  ("plan", "_plan_model"))
            }
            base["engine_models"] = {k: v for k, v in models.items() if v}
        return base


__all__ = [
    "CORE_SERVICES",
    "ConciergeAgent",
    "NIGHT_END_HOUR",
    "NIGHT_START_HOUR",
    "PERIPHERAL_SERVICES",
    "RESOLVABLE_INTENTS",
    "Resolution",
    "SELF_CHECK_INFORMATIONAL",
    "SERVICE_DEGRADED_DEDUP_SECONDS",
    "SERVICE_NAME_TO_KEY",
    "is_night",
    "reset_service_degraded_dedup",
    "resolve_intent",
    "service_key",
]
