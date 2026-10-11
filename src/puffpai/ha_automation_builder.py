"""Home Assistant automations the owner asks for, built from a FIXED schema.

A request such as "a new workflow that turns off the living room lamp if no
motion after midnight every night" must reach the provisioning path whatever
words it uses, and a small model must never write raw Home Assistant JSON.
This module is the deterministic pipeline for that:

1. :func:`is_author_request` / :func:`is_rule_shaped` decide, from the words
   alone, that a turn asks for a NEW standing rule. No model picks this route.
2. :func:`parse_rules` fills a small closed schema (action, target phrase,
   trigger type, minutes, sun offset, time). An optional model slot-filler may
   fill the SAME schema when the parser cannot; its output is validated by
   :func:`validate_spec` exactly like the parser's, and any entity id it
   offers is ignored.
3. :func:`resolve_rule` turns phrases into entity ids from the LIVE Home
   Assistant state list only. Nothing is invented: no match means a question.
4. :func:`build_payload` renders the automation. The allowlist (light, switch,
   fan, scene actions; time, sun, state triggers) is enforced again in code by
   :func:`puffpai.ha_provisioning._validate_payload`, at propose AND at apply.
5. The result is filed through the existing ``ha_provision`` consent path
   (:func:`puffpai.ha_provisioning.propose_ha_change`). Nothing here writes to
   Home Assistant, touches a credential, or approves anything.

A duplicate guard compares each new rule with the automations Home Assistant
already has and asks "replace it or keep both?" instead of silently adding a
second one. "Replace" means: create the new one and switch the old one OFF
(never delete it), and that is spelled out in the consent text the owner
approves.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# ── the closed schema ───────────────────────────────────────────────

ACTIONS = ("turn_on", "turn_off")
TRIGGER_TYPES = ("no_motion", "motion", "door_open", "door_close", "time", "sun")
SUN_EVENTS = ("sunset", "sunrise")
WINDOWS = ("after_midnight",)

#: Action domains an owner automation may touch. Mirrors
#: :data:`puffpai.ha_provisioning.AUTOMATION_ACTION_DOMAINS`, which is the
#: enforcing copy.
ACTION_DOMAINS = ("light", "switch", "fan", "scene")

DEFAULT_NO_MOTION_MINUTES = 30
MAX_MINUTES = 24 * 60
MAX_SUN_OFFSET_MINUTES = 6 * 60
MAX_TARGETS = 12
DRAFT_TTL_SECONDS = 15 * 60
OBJECT_PREFIX = "puffpai_"
MAX_OBJECT_ID = 64

#: binary_sensor device classes per sensor kind.
_SENSOR_CLASSES = {
    "motion": ("motion", "occupancy", "presence"),
    "door": ("door", "opening", "garage_door", "window"),
}
_SENSOR_WORDS = {
    "motion": ("motion", "occupancy", "presence"),
    "door": ("door", "contact", "opening"),
}

# ── request detection (deterministic) ───────────────────────────────

_NOUN = r"(?:automations?|workflows?|routines?)"
_AUTHOR_RE = re.compile(
    r"\b(?:create|make|add|build|set\s*up|setup|write|draft|program|do)\b"
    r"(?:\W+\w+){0,5}?\W+" + _NOUN + r"\b"
    r"|\bnew\s+(?:automation|workflow|routine|rule)s?\b"
    r"|\b(?:create|make|add|set\s*up|setup|write)\s+(?:a\s+|an\s+|another\s+|"
    r"the\s+|my\s+)?(?:new\s+)?rules?\b"
    r"|\b" + _NOUN + r"\s+(?:that|which)\b",
    re.IGNORECASE)

_ACTION_RE = re.compile(
    r"\b(?:turn|switch|shut)(?:s|ed|ing)?\s+(?P<a1>on|off)\b"
    r"|\b(?:turn|switch)(?:s|ed|ing)?\s+(?P<t2>(?:the\s+|my\s+)?[a-z0-9' ]{1,60}?)"
    r"\s+(?P<a2>on|off)\b",
    re.IGNORECASE)

_RECUR_RE = re.compile(
    r"\b(?:every|each)\s+(?:night|day|evening|morning|afternoon|weekday|"
    r"single\s+(?:night|day))\b|\bnightly\b|\bdaily\b|\bautomatically\b",
    re.IGNORECASE)
_NO_MOTION_RE = re.compile(
    r"\bno\s+(?:motion|movement|one\s+moves|occupancy)\b"
    r"|\b(?:nobody|no\s*one|nothing)\s+(?:is\s+)?(?:moves?|moving|there|around)\b"
    r"|\bmotion\s+(?:stops|has\s+stopped|clears|is\s+clear)\b"
    r"|\bwithout\s+(?:any\s+)?(?:motion|movement)\b",
    re.IGNORECASE)
_MOTION_RE = re.compile(
    r"\b(?:if|when|whenever|once)\b[^.?!]{0,30}\b(?:motion|movement|someone|"
    r"somebody|anyone)\b",
    re.IGNORECASE)
_DOOR_RE = re.compile(
    r"\b(?:if|when|whenever|once)\s+(?:the\s+|my\s+)?(?P<door>[a-z ]{0,30}?)\s*"
    r"door\s+(?:is\s+)?(?P<how>opens|opened|open|closes|closed|shuts|shut)\b",
    re.IGNORECASE)
_AFTER_MIDNIGHT_RE = re.compile(r"\bafter\s+midnight\b", re.IGNORECASE)
_DUR = (r"(?:(?P<n>\d{1,3})\s*(?P<u>minutes?|mins?|hours?|hrs?)"
        r"|(?:a\s+)?half\s+(?:an\s+)?hour|(?:an|one)\s+hour)")
_SUN_RE = re.compile(
    r"(?:(?P<dur>" + _DUR + r")\s+)?(?P<rel>before|after)\s+(?P<ev>sunset|sunrise|dusk|dawn)"
    r"|\bat\s+(?P<ev2>sunset|sunrise|dusk|dawn)\b",
    re.IGNORECASE)
_TIME_RE = re.compile(
    r"\bat\s+(?:(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?)?"
    r"|(?P<word>midnight|noon))\b",
    re.IGNORECASE)
_MINUTES_RE = re.compile(r"\b(?:for\s+)?" + _DUR, re.IGNORECASE)
_PRONOUN_RE = re.compile(r"\b(?:them|they|it|those|these)\b", re.IGNORECASE)
_CLAUSE_SPLIT_RE = re.compile(r"[.?!;]+|\b(?:and\s+)?also\b|\bplus\b",
                              re.IGNORECASE)
_TARGET_STOP_RE = re.compile(
    r"\s+(?:if|when|whenever|after|before|at|every|each|automatically|unless|"
    r"once|nightly|daily|for|in\s+the|until)\b|[,]",
    re.IGNORECASE)

_REPLACE_RE = re.compile(r"\b(?:replace|swap|use\s+the\s+new|switch\s+the\s+old)",
                         re.IGNORECASE)
_KEEP_RE = re.compile(r"\b(?:keep\s+both|both|keep\s+(?:it|them|the\s+old)|"
                      r"add\s+(?:it|them)\s+anyway|anyway|leave\s+(?:it|them))\b",
                      re.IGNORECASE)
_CANCEL_RE = re.compile(r"^\s*(?:no|nope|cancel|stop|never\s*mind|nevermind|"
                        r"forget\s+it|don'?t)\b", re.IGNORECASE)

_NORMALISE = (
    (re.compile(r"\blivingroom\b"), "living room"),
    (re.compile(r"\bdiningroom\b"), "dining room"),
    (re.compile(r"\bfamilyroom\b"), "family room"),
    (re.compile(r"\bfrontdoor\b"), "front door"),
    (re.compile(r"\s+"), " "),
)


def _norm(text: str) -> str:
    low = (text or "").lower().replace("’", "'")
    for rx, rep in _NORMALISE:
        low = rx.sub(rep, low)
    return low.strip()


#: A question ABOUT automations is a read, never a proposal. "can/could/would/
#: will you ..." are requests and stay out of this.
_QUESTION_RE = re.compile(
    r"^\s*(?:do|does|did|is|are|was|were|have|has|had|what|what's|whats|"
    r"which|who|whom|whose|how|why|where|should|shall|am|any|"
    r"can\s+i|could\s+i|can\s+we|tell\s+me\s+about|show\s+me|list|"
    r"describe|explain)\b",
    re.IGNORECASE)
_DELETE_RE = re.compile(
    r"\b(?:delete|remove|get\s+rid\s+of|disable|deactivate|turn\s+off|"
    r"switch\s+off|stop|cancel|kill)\s+(?:the\s+|my\s+|that\s+|this\s+)?"
    r"(?:\w+\s+){0,3}?(?:automations?|routines?|workflows?|rules?)\b",
    re.IGNORECASE)
_NOT_A_COMMAND_RE = re.compile(
    r"\b(?:forget|forgot|remember|remembered|forgetting)\s+to\b",
    re.IGNORECASE)
_NOUN_RE = re.compile(r"\b(?:automations?|workflows?|routines?)\b", re.IGNORECASE)


def is_question(text: str) -> bool:
    return bool(_QUESTION_RE.search(_norm(text)))


def is_automation_question(text: str) -> bool:
    """"do I have an automation for X?", "do my automations work?" -> a READ."""
    low = _norm(text)
    return bool(low and is_question(low) and _NOUN_RE.search(low)
                and not is_delete_request(low))


def is_delete_request(text: str) -> bool:
    return bool(_DELETE_RE.search(_norm(text)))


def is_author_request(text: str) -> bool:
    """True when the words ask PuffPAI to MAKE a new automation. Pure."""
    low = _norm(text)
    if not low or len(low) > 600:
        return False
    if is_question(low) or is_delete_request(low):
        return False
    try:
        from puffpai.agents.automations_read import is_automations_question
        if is_automations_question(low):
            return False
    except Exception:                               # pragma: no cover
        pass
    return bool(_AUTHOR_RE.search(low))


def is_rule_shaped(text: str) -> bool:
    """True for a standing rule: an on/off action plus a recurring or
    sensor-conditioned cue. "turn off the lights now" is NOT one. Pure."""
    low = _norm(text)
    if not low or len(low) > 600 or not _ACTION_RE.search(low):
        return False
    if is_question(low) or is_delete_request(low) or _NOT_A_COMMAND_RE.search(low):
        return False
    if _NO_MOTION_RE.search(low) or _MOTION_RE.search(low) or _DOOR_RE.search(low):
        return True
    return bool(_RECUR_RE.search(low))


def claims_turn(text: str) -> bool:
    """Should the automation author own this turn (before compound/planner)?"""
    return (pending_draft() is not None or is_author_request(text)
            or is_rule_shaped(text) or is_delete_request(text)
            or is_automation_question(text))


# ── parsing into the schema ─────────────────────────────────────────

def _minutes_from(match) -> Optional[int]:
    if match is None:
        return None
    text = match.group(0).lower()
    n = match.groupdict().get("n")
    unit = (match.groupdict().get("u") or "").lower()
    if n:
        value = int(n)
        return value * 60 if unit.startswith("h") else value
    if "half" in text:
        return 30
    if "hour" in text:
        return 60
    return None


def _parse_clause(clause: str, previous_target: str | None) -> Optional[dict]:
    low = _norm(clause)
    act = _ACTION_RE.search(low)
    if not act:
        return None
    action = "turn_" + (act.group("a1") or act.group("a2")).lower()
    target = ""
    if act.group("t2"):
        target = act.group("t2")
    else:
        tail = low[act.end():]
        stop = _TARGET_STOP_RE.search(tail)
        target = tail[:stop.start()] if stop else tail
    target = target.strip(" ,")
    if _PRONOUN_RE.fullmatch(target or "") or not re.search(r"[a-z]", target or ""):
        target = ""
    if not target and previous_target and _PRONOUN_RE.search(low):
        target = previous_target
    if not target and previous_target:
        target = previous_target

    spec: dict[str, Any] = {"action": action, "target_phrase": target,
                            "trigger": None, "window": None,
                            "minutes": None, "minutes_defaulted": False,
                            "sun_event": None, "sun_offset_minutes": 0,
                            "time": None, "sensor_phrase": None}
    rest = low
    sun = _SUN_RE.search(rest)
    if sun:
        ev = (sun.group("ev") or sun.group("ev2") or "").lower()
        ev = {"dusk": "sunset", "dawn": "sunrise"}.get(ev, ev)
        offset = 0
        if sun.group("rel"):
            dur = _minutes_from(_MINUTES_RE.search(sun.group("dur") or "")) \
                if sun.group("dur") else 0
            offset = -(dur or 0) if sun.group("rel").lower() == "before" else (dur or 0)
        spec["sun_event"], spec["sun_offset_minutes"] = ev, offset
        rest = rest[:sun.start()] + " " + rest[sun.end():]
    if _AFTER_MIDNIGHT_RE.search(rest):
        spec["window"] = "after_midnight"
    door = _DOOR_RE.search(rest)
    if _NO_MOTION_RE.search(rest):
        spec["trigger"] = "no_motion"
        mins = _minutes_from(_MINUTES_RE.search(rest))
        spec["minutes"] = mins or DEFAULT_NO_MOTION_MINUTES
        spec["minutes_defaulted"] = mins is None
    elif door:
        how = door.group("how").lower()
        spec["trigger"] = "door_open" if how.startswith("open") else "door_close"
        spec["sensor_phrase"] = (door.group("door") or "").strip() + " door"
    elif _MOTION_RE.search(rest):
        spec["trigger"] = "motion"
    elif sun:
        spec["trigger"] = "sun"
    else:
        tm = _TIME_RE.search(rest)
        if tm:
            spec["trigger"] = "time"
            spec["time"] = _clock(tm)
    if spec["trigger"] is None:
        return None
    return spec


def _clock(match) -> Optional[str]:
    word = (match.group("word") or "").lower()
    if word == "midnight":
        return "00:00"
    if word == "noon":
        return "12:00"
    if not match.group("h"):
        return None
    hour = int(match.group("h"))
    minute = int(match.group("m") or 0)
    ap = (match.group("ap") or "").lower().replace(".", "")
    if ap == "pm" and hour < 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_rules(text: str) -> list[dict]:
    """Every standing rule in *text*, in order, as schema dicts. Pure."""
    rules: list[dict] = []
    previous = None
    for clause in _CLAUSE_SPLIT_RE.split(_norm(text)):
        if not clause or not clause.strip():
            continue
        spec = _parse_clause(clause, previous)
        if spec is None:
            continue
        if not spec["target_phrase"] and previous:
            spec["target_phrase"] = previous
        previous = spec["target_phrase"] or previous
        try:
            rules.append(validate_spec(spec))
        except ValueError as exc:
            logger.debug("automation builder: clause dropped: %s", exc)
    return rules


def validate_spec(spec: Any) -> dict:
    """Enforce the closed schema on a parsed OR model-filled rule.

    Raises ValueError on anything outside it. Entity ids are never accepted
    here: a model that offers one has it dropped, and resolution happens in
    :func:`resolve_rule` from the live state list only.
    """
    if not isinstance(spec, dict):
        raise ValueError("rule must be an object")
    action = spec.get("action")
    if action not in ACTIONS:
        raise ValueError("action must be turn_on or turn_off")
    trigger = spec.get("trigger")
    if trigger not in TRIGGER_TYPES:
        raise ValueError("unsupported trigger")
    target = str(spec.get("target_phrase") or "").strip()[:80]
    if "." in target and re.search(r"\b[a-z_]+\.[a-z0-9_]+\b", target):
        target = re.sub(r"\b[a-z_]+\.([a-z0-9_]+)\b",
                        lambda m: m.group(1).replace("_", " "), target)
    window = spec.get("window")
    if window not in (None,) + WINDOWS:
        raise ValueError("unsupported time window")
    out = {"action": action, "target_phrase": target, "trigger": trigger,
           "window": window, "minutes": None, "minutes_defaulted": False,
           "sun_event": None, "sun_offset_minutes": 0, "time": None,
           "sensor_phrase": None}
    if trigger == "no_motion":
        mins = spec.get("minutes")
        if isinstance(mins, bool) or not isinstance(mins, int) \
                or not (1 <= mins <= MAX_MINUTES):
            mins, defaulted = DEFAULT_NO_MOTION_MINUTES, True
        else:
            defaulted = bool(spec.get("minutes_defaulted"))
        out["minutes"], out["minutes_defaulted"] = mins, defaulted
    if trigger == "sun":
        ev = spec.get("sun_event")
        if ev not in SUN_EVENTS:
            raise ValueError("sun trigger needs sunset or sunrise")
        off = spec.get("sun_offset_minutes") or 0
        if isinstance(off, bool) or not isinstance(off, int) \
                or abs(off) > MAX_SUN_OFFSET_MINUTES:
            raise ValueError("sun offset out of range")
        out["sun_event"], out["sun_offset_minutes"] = ev, off
    if trigger == "time":
        tm = spec.get("time")
        if not isinstance(tm, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", tm):
            raise ValueError("time must be HH:MM")
        out["time"] = tm
    if trigger in ("door_open", "door_close", "motion", "no_motion"):
        phrase = spec.get("sensor_phrase")
        out["sensor_phrase"] = (str(phrase).strip()[:60] or None) if phrase else None
    return out


#: The prompt for the optional model slot-filler. The model returns phrases and
#: enums only; code validates and resolves.
SLOT_PROMPT = (
    "Extract Home Assistant automation rules from the request below. Reply with "
    "ONLY a JSON list. Each item: {\"action\": \"turn_on\"|\"turn_off\", "
    "\"target_phrase\": words naming the lights/switches/fans/scene, "
    "\"trigger\": \"no_motion\"|\"motion\"|\"door_open\"|\"door_close\"|\"time\"|\"sun\", "
    "\"window\": \"after_midnight\" or null, \"minutes\": integer or null, "
    "\"sun_event\": \"sunset\"|\"sunrise\" or null, \"sun_offset_minutes\": "
    "integer (negative = before), \"time\": \"HH:MM\" or null, "
    "\"sensor_phrase\": words naming the sensor or null}. Never invent entity "
    "ids. If it is not a standing rule, reply [].\n\nRequest: ")


def fill_with_model(text: str, llm: Callable[[str], str] | None) -> list[dict]:
    """Let a model fill the schema when the parser could not. Never raises."""
    if llm is None:
        return []
    try:
        raw = llm(SLOT_PROMPT + _norm(text)[:600]) or ""
    except Exception as exc:
        logger.debug("automation builder: slot filler failed: %s",
                     type(exc).__name__)
        return []
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except (ValueError, TypeError):
        return []
    rules = []
    for item in items if isinstance(items, list) else []:
        try:
            rules.append(validate_spec(item))
        except ValueError:
            continue
    return rules[:4]


# ── resolution against LIVE entities ────────────────────────────────

_GENERIC = {"the", "my", "a", "an", "all", "both", "light", "lights", "lamp",
            "lamps", "bulb", "bulbs", "in", "on", "of", "and", "to", "our",
            "switch", "switches", "plug", "plugs", "fan", "fans", "scene",
            "smart", "zigbee", "them", "it", "those", "these", "turn", "off",
            "door", "sensor"}
_INDICATOR_RE = re.compile(r"\b(?:led|status|ring|indicator|backlight)\b")
_RISKY_SWITCH_RE = re.compile(
    r"(?<![a-z])(?:garage|doors?|locks?|gates?|alarms?|permit|pair|pairing|"
    r"join|joining|opener|siren|valves?|security|arm|armed|disarm)(?![a-z])",
    re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _norm(str(text or "").replace("_", " ")))


def _states_index(states: Iterable[dict]) -> list[dict]:
    out = []
    for raw in states or []:
        if not isinstance(raw, dict):
            continue
        eid = raw.get("entity_id")
        if not isinstance(eid, str) or "." not in eid:
            continue
        attrs = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
        name = str(attrs.get("friendly_name") or eid)
        out.append({"entity_id": eid, "domain": eid.split(".", 1)[0],
                    "name": name, "state": raw.get("state"), "attrs": attrs,
                    "tokens": set(_tokens(name)) | set(_tokens(eid.split(".", 1)[1]))})
    return out


def _target_domains(phrase: str) -> tuple[str, ...]:
    low = _norm(phrase)
    if re.search(r"\bscene\b", low):
        return ("scene",)
    if re.search(r"\bfans?\b", low):
        return ("fan",)
    if re.search(r"\b(?:switch(?:es)?|plugs?|outlets?)\b", low):
        return ("switch",)
    return ("light",)


def resolve_targets(phrase: str, index: list[dict]) -> list[dict]:
    want = [t for t in _tokens(phrase) if t not in _GENERIC]
    if not want:
        return []
    domains = _target_domains(phrase)
    wants_indicator = bool(_INDICATOR_RE.search(_norm(phrase)))
    scored = []
    for ent in index:
        if ent["domain"] not in domains:
            continue
        if ent["domain"] == "light" and not wants_indicator \
                and _INDICATOR_RE.search(_norm(ent["name"] + " " + ent["entity_id"])):
            continue
        score = sum(1 for t in want if t in ent["tokens"])
        if score:
            scored.append((score, ent))
    if not scored:
        return []
    best = max(s for s, _ in scored)
    return sorted((e for s, e in scored if s == best), key=lambda e: e["name"])


def resolve_sensor(kind: str, phrase: str | None, targets: list[dict],
                   index: list[dict]) -> list[dict]:
    classes = _SENSOR_CLASSES[kind]
    words = _SENSOR_WORDS[kind]
    cands = [e for e in index if e["domain"] == "binary_sensor"
             and (e["attrs"].get("device_class") in classes
                  or any(w in e["tokens"] for w in words))]
    if not cands:
        return []
    if phrase:
        area = [t for t in _tokens(phrase) if t not in _GENERIC and t not in words]
    else:
        area = []
        for t in targets:
            area.extend(x for x in _tokens(t["name"] + " " + t["entity_id"].split(".", 1)[1])
                        if x not in _GENERIC and not x.isdigit())
    area = list(dict.fromkeys(area))
    scored = []
    for ent in cands:
        score = sum(2 for t in area if t in ent["tokens"])
        if kind in ent["tokens"] or words[0] in ent["tokens"]:
            score += 1
        if ent["state"] not in ("unavailable", "unknown"):
            score += 1
        scored.append((score, ent))
    best = max(s for s, _ in scored)
    if area and best < 3:            # nothing shares the room: ask, don't guess
        return sorted((e for _, e in scored), key=lambda e: e["name"])[:4] + [None]
    return [e for s, e in scored if s == best]


@dataclass
class Resolved:
    spec: dict
    targets: list[dict] = field(default_factory=list)
    sensor: Optional[dict] = None
    question: Optional[str] = None
    options: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)


def resolve_rule(spec: dict, index: list[dict]) -> Resolved:
    res = Resolved(spec=spec)
    targets = resolve_targets(spec["target_phrase"], index)
    if not targets:
        what = spec["target_phrase"] or "which lights"
        res.question = (f"I couldn't find {what!r} in Home Assistant. Which "
                        f"lights, switches or fans should it control?")
        return res
    if len(targets) > MAX_TARGETS:
        res.question = (f"That matches {len(targets)} devices. Name the ones "
                        f"you mean, for example the room and the lamp.")
        return res
    risky = [t for t in targets if t["domain"] == "switch" and _RISKY_SWITCH_RE.search(
        " ".join([t["entity_id"], t["name"], str(t["attrs"].get("device_class") or "")]))]
    if risky:
        res.question = (f"I won't put {risky[0]['name']} in an automation: it "
                        f"looks like a garage, door, lock, gate, alarm or "
                        f"pairing control.")
        return res
    res.targets = targets
    kind = {"no_motion": "motion", "motion": "motion", "door_open": "door",
            "door_close": "door"}.get(spec["trigger"])
    if kind:
        found = resolve_sensor(kind, spec.get("sensor_phrase"), targets, index)
        if not found:
            res.question = (f"I don't see a {kind} sensor in Home Assistant, so I "
                            f"can't watch for that.")
            return res
        if None in found or len(found) > 1:
            res.options = [e for e in found if e is not None]
            names = " or ".join(e["name"] for e in res.options)
            res.question = f"Which {kind} sensor should I watch: {names}?"
            return res
        res.sensor = found[0]
    return res


# ── rendering ───────────────────────────────────────────────────────

def _names(ents: list[dict]) -> str:
    names = [e["name"] for e in ents]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _clock_words(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def _offset_words(minutes: int, event: str) -> str:
    if not minutes:
        return f"at {event}"
    mins = abs(minutes)
    amount = ("half an hour" if mins == 30 else "an hour" if mins == 60
              else f"{mins} minutes")
    return f"{amount} {'before' if minutes < 0 else 'after'} {event}"


def summary(res: Resolved) -> str:
    """One plain sentence describing what will be created."""
    s = res.spec
    verb = "turn on" if s["action"] == "turn_on" else "turn off"
    what = _names(res.targets)
    sensor = res.sensor["name"] if res.sensor else ""
    if s["trigger"] == "no_motion":
        lead = ("Every night after midnight, if" if s["window"] == "after_midnight"
                else "Whenever")
        mins = s["minutes"]
        tail = "minute" if mins == 1 else "minutes"
        if s["window"] == "after_midnight":
            return (f"{lead} {sensor} has seen nothing for {mins} {tail}, "
                    f"{verb} {what}.")
        return f"Whenever {sensor} has seen nothing for {mins} {tail}, {verb} {what}."
    if s["trigger"] == "motion":
        return f"Whenever {sensor} sees motion, {verb} {what}."
    if s["trigger"] in ("door_open", "door_close"):
        how = "opens" if s["trigger"] == "door_open" else "closes"
        return f"Whenever {sensor} {how}, {verb} {what}."
    if s["trigger"] == "sun":
        return (f"Every day {_offset_words(s['sun_offset_minutes'], s['sun_event'])}, "
                f"{verb} {what}.")
    return f"Every day at {_clock_words(s['time'])}, {verb} {what}."


def _slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")


def alias_and_object_id(res: Resolved) -> tuple[str, str]:
    """An alias whose Home Assistant slug IS the object id.

    HA derives an automation's entity id from its alias, not from the config
    id, so the two are kept identical for the provisioning read-back.
    """
    s = res.spec
    onoff = "on" if s["action"] == "turn_on" else "off"
    when = {"no_motion": "no motion", "motion": "on motion",
            "door_open": "door opens", "door_close": "door closes",
            "sun": s.get("sun_event") or "sun", "time": "at " + (s.get("time") or "").replace(":", "")
            }[s["trigger"]]
    if s["trigger"] == "no_motion" and s["window"] == "after_midnight":
        when = "after midnight no motion"
    if s["trigger"] == "sun" and s["sun_offset_minutes"]:
        when = (f"{abs(s['sun_offset_minutes'])} min "
                f"{'before' if s['sun_offset_minutes'] < 0 else 'after'} {s['sun_event']}")
    names = " ".join(e["name"] for e in res.targets)
    for candidate in (names, f"{len(res.targets)} lights" if len(res.targets) > 1
                      else names[:30]):
        alias = f"PuffPAI {candidate} {onoff} {when}"
        alias = re.sub(r"[^A-Za-z0-9 ]+", " ", alias)
        alias = re.sub(r"\s+", " ", alias).strip()
        oid = _slug(alias)
        if len(oid) <= MAX_OBJECT_ID:
            return alias, oid
    oid = _slug(alias)[:MAX_OBJECT_ID].strip("_")
    return alias[:MAX_OBJECT_ID], oid


def build_payload(res: Resolved) -> dict:
    """The automation body. Only allowlisted shapes; no templates."""
    s = res.spec
    alias, _oid = alias_and_object_id(res)
    domain_services = {}
    for ent in res.targets:
        domain_services.setdefault(ent["domain"], []).append(ent["entity_id"])
    actions = []
    for domain, ids in sorted(domain_services.items()):
        service = "turn_on" if domain == "scene" else s["action"]
        actions.append({"service": f"{domain}.{service}",
                        "target": {"entity_id": sorted(ids)}})
    triggers: list[dict] = []
    conditions: list[dict] = []
    sensor = res.sensor["entity_id"] if res.sensor else None
    if s["trigger"] == "no_motion":
        hold = {"hours": 0, "minutes": int(s["minutes"]), "seconds": 0}
        triggers.append({"platform": "state", "entity_id": sensor, "to": "off",
                         "for": hold})
        if s["window"] == "after_midnight":
            triggers.insert(0, {"platform": "time", "at": "00:00:00"})
            conditions.append({"condition": "state", "entity_id": sensor,
                               "state": "off", "for": hold})
            conditions.append({"condition": "sun", "before": "sunrise"})
    elif s["trigger"] == "motion":
        triggers.append({"platform": "state", "entity_id": sensor, "to": "on"})
    elif s["trigger"] in ("door_open", "door_close"):
        triggers.append({"platform": "state", "entity_id": sensor,
                         "to": "on" if s["trigger"] == "door_open" else "off"})
    elif s["trigger"] == "sun":
        off = int(s["sun_offset_minutes"])
        sign = "-" if off < 0 else ""
        off = abs(off)
        triggers.append({"platform": "sun", "event": s["sun_event"],
                         "offset": f"{sign}{off // 60:02d}:{off % 60:02d}:00"})
    else:
        triggers.append({"platform": "time", "at": f"{s['time']}:00"})
    return {"alias": alias,
            "description": summary(res) + " Made by PuffPAI with your approval.",
            "mode": "single", "trigger": triggers, "condition": conditions,
            "action": actions}


# ── duplicate guard ─────────────────────────────────────────────────

_TRIGGER_WORDS = {
    "no_motion": ("motion", "occupancy", "presence", "vacancy"),
    "motion": ("motion", "occupancy", "presence"),
    "door_open": ("door", "open", "opens"),
    "door_close": ("door", "close", "closed"),
    "sun": ("sunset", "sunrise", "dusk", "dawn"),
    "time": ("midnight", "night", "nightly"),
}


def find_duplicates(res: Resolved, index: list[dict]) -> list[dict]:
    """Existing automations that look like the same job. Heuristic, on names.

    The restricted account cannot read automation configs, so this compares
    the friendly name with the new rule's target words and trigger words.
    It errs toward ASKING: a false alarm costs one question, a miss costs a
    silent duplicate.
    """
    s = res.spec
    target_words = set()
    for ent in res.targets:
        target_words |= {t for t in _tokens(ent["name"]) if t not in _GENERIC
                         and not t.isdigit()}
    trig = set(_TRIGGER_WORDS.get(s["trigger"], ()))
    if s["trigger"] == "sun" and s.get("sun_event"):
        trig = {s["sun_event"], "dusk" if s["sun_event"] == "sunset" else "dawn"}
    onoff = "on" if s["action"] == "turn_on" else "off"
    _alias, oid = alias_and_object_id(res)
    for ent in res.targets:
        target_words |= {t for t in _tokens(ent["entity_id"].split(".", 1)[1])
                         if t not in _GENERIC and not t.isdigit()}
    target_words -= {"room"}
    dups = []
    for ent in index:
        if ent["domain"] != "automation":
            continue
        if ent["entity_id"] == f"automation.{oid}":
            dups.append(ent)
            continue
        toks = ent["tokens"]
        # Same trigger family AND a shared target word. A name match on
        # "lights"/"off" alone would match "Garage motion lights off" to an
        # unrelated lamp rule.
        if (trig & toks) and (target_words & toks):
            dups.append(ent)
    return dups


# ── the pending draft (one per household) ───────────────────────────

_LOCK = threading.RLock()
_draft: dict | None = None


def pending_draft() -> dict | None:
    global _draft
    with _LOCK:
        if _draft and time.monotonic() > _draft.get("expires", 0):
            _draft = None
        return _draft


def clear_draft() -> None:
    global _draft
    with _LOCK:
        _draft = None


def _set_draft(stage: str, items: list, question: str,
               conversation_id: str | None = None) -> None:
    global _draft
    with _LOCK:
        _draft = {"stage": stage, "items": items, "question": question,
                  "conversation_id": conversation_id,
                  "expires": time.monotonic() + DRAFT_TTL_SECONDS}


# ── the turn handler ────────────────────────────────────────────────

ASK_FOR_RULE = ("Sure, I can make that in Home Assistant. Tell me what it should "
                "do, for example: \"turn off the sofa lights if there's no "
                "motion after midnight\".")

APPROVE_TAIL = " Approve? Say yes, or tap Approve on the dashboard."
APPROVE_BOTH_TAIL = (" Approve both? Say yes to both, or tap Approve on the "
                     "dashboard.")
APPROVE_ALL_TAIL = (" Approve them all? Say yes to all, or tap Approve on the "
                    "dashboard.")


def _propose(items: list[Resolved], replace: bool,
             conversation_id: str | None = None) -> str:
    from puffpai import ha_provisioning as _prov
    import uuid as _uuid
    sentences, filed = [], 0
    batch = _uuid.uuid4().hex[:12] if len(items) > 1 else None
    for res in items:
        alias, oid = alias_and_object_id(res)
        payload = build_payload(res)
        replaces = [d["entity_id"] for d in res.duplicates
                    if d["entity_id"] != f"automation.{oid}"] if replace else []
        reason = summary(res)
        if replaces:
            reason += (" It also switches off your existing "
                       + _names([{"name": d["name"]} for d in res.duplicates
                                 if d["entity_id"] in replaces])
                       + " (it stays in Home Assistant, just turned off).")
        try:
            result = _prov.propose_ha_change("automation", oid, payload, reason,
                                             requested_by="steward",
                                             replaces=replaces,
                                             conversation_id=conversation_id,
                                             batch=batch)
        except _prov.ProvisioningError as exc:
            sentences.append(f"I can't make that one: {exc}")
            continue
        if not result.get("proposed"):
            sentences.append(result.get("message") or "I couldn't file that.")
            continue
        filed += 1
        sentences.append(summary(res) + (
            " I'll switch off " + _names([{"name": d["name"]} for d in res.duplicates
                                          if d["entity_id"] in replaces])
            + " (not delete it)." if replaces else ""))
    clear_draft()
    text = " ".join(sentences)
    if filed > 1:
        text += APPROVE_BOTH_TAIL if filed == 2 else APPROVE_ALL_TAIL
    elif filed:
        text += APPROVE_TAIL
    return text


def _dup_question(items: list[Resolved]) -> str:
    dups = []
    for res in items:
        for d in res.duplicates:
            if d["name"] not in dups:
                dups.append(d["name"])
    quoted = " and ".join(f"'{n}'" for n in dups)
    one = len(dups) == 1
    return (f"You already have {quoted}, which "
            f"{'looks like it does' if one else 'look like they do'} the same "
            f"{'job' if one else 'jobs'}. Should I replace "
            f"{'it' if one else 'them'} (I'll make the new "
            f"{'one' if len(items) == 1 else 'ones'} and switch the old "
            f"{'one' if one else 'ones'} off, not delete "
            f"{'it' if one else 'them'}), or keep both?")


def _history_rule_text(history) -> str | None:
    for turn in reversed(list(history or [])[-8:]):
        if not isinstance(turn, dict):
            continue
        text = turn.get("user") or (turn.get("content") if turn.get("role") == "user" else None)
        if isinstance(text, str) and is_rule_shaped(text):
            return text
    return None


def _answer_draft(text: str, draft: dict, states) -> Optional[str]:
    low = _norm(text)
    stage = draft["stage"]
    # A cancel is a SHORT "no / cancel / never mind". Her live turn 2 began
    # "no you have the rights ... you do it", which is the opposite.
    if (_CANCEL_RE.search(low) and len(low.split()) <= 4
            and not _REPLACE_RE.search(low) and not is_author_request(text)):
        clear_draft()
        return "OK, I won't make it."
    if stage == "duplicate":
        if _REPLACE_RE.search(low):
            return _propose(draft["items"], replace=True,
                            conversation_id=draft.get("conversation_id"))
        if _KEEP_RE.search(low):
            return _propose(draft["items"], replace=False,
                            conversation_id=draft.get("conversation_id"))
        if is_rule_shaped(text):
            return None                      # a new rule supersedes
        return "Should I replace the old one, or keep both?" \
            if len(draft["items"]) == 1 else draft["question"]
    if stage == "sensor":
        res: Resolved = draft["items"][0]
        for opt in draft.get("options", []):
            if set(_tokens(opt["name"])) & set(_tokens(low)) - _GENERIC:
                res.sensor = opt
                res.question = None
                return _continue(draft["items"], states,
                                 draft.get("conversation_id"))
        if is_rule_shaped(text):
            return None
        return draft["question"]
    return None


def _continue(items: list[Resolved], states,
              conversation_id: str | None = None) -> str:
    index = _states_index(states)
    for res in items:
        res.duplicates = find_duplicates(res, index)
    if any(r.duplicates for r in items):
        question = " ".join(summary(r) for r in items) + " " + _dup_question(items)
        _set_draft("duplicate", items, _dup_question(items), conversation_id)
        return question
    return _propose(items, replace=False, conversation_id=conversation_id)


DELETE_NONE = ("I don't see an automation like that in Home Assistant, so "
               "there's nothing to switch off.")


def _handle_delete(text: str, states, conversation_id: str | None) -> str:
    """Puff never deletes an automation. It offers to switch one OFF, through
    the same ha_provision consent path (kind ``automation_off``)."""
    index = _states_index(states or [])
    autos = [e for e in index if e["domain"] == "automation"]
    want = [t for t in _tokens(_DELETE_RE.sub(" ", _norm(text)))
            if t not in _GENERIC and t not in ("that", "which", "turns",
                                               "turn", "the", "on")]
    scored = sorted(((sum(1 for t in want if t in e["tokens"]), e) for e in autos),
                    key=lambda x: -x[0])
    best = scored[0][0] if scored else 0
    matches = [e for sc, e in scored if sc == best and sc > 0]
    lead = "I can't delete automations, only switch them off. "
    if not matches:
        return lead + DELETE_NONE
    if len(matches) > 1:
        names = " or ".join(f"'{e['name']}'" for e in matches[:4])
        return lead + f"Which one should I switch off: {names}?"
    target = matches[0]
    from puffpai import ha_provisioning as _prov
    out = _prov.propose_automation_off(target["entity_id"], target["name"],
                                       conversation_id=conversation_id)
    if not out.get("proposed"):
        return out.get("message") or "I couldn't file that."
    return (lead + f"Should I switch off '{target['name']}'? It stays in Home "
            f"Assistant, just turned off." + APPROVE_TAIL)


def handle_turn(text: str, states: list | None, *, history=None,
                llm: Callable[[str], str] | None = None,
                conversation_id: str | None = None) -> Optional[str]:
    """Own an automation-authoring turn, or return None to leave it alone.

    *history* is accepted for callers but deliberately NOT mined for an old
    rule: a rule is only ever built from the current turn, or confirmed from a
    pending draft.
    """
    if is_automation_question(text):
        return None                     # the automations-read answer owns it
    if is_delete_request(text):
        clear_draft()
        if states is None:
            return ("I can't read Home Assistant right now, so I can't find "
                    "that automation.")
        return _handle_delete(text, states, conversation_id)
    draft = pending_draft()
    if draft is not None:
        answer = _answer_draft(text, draft, states)
        if answer is not None:
            return answer
        if not (is_author_request(text) or is_rule_shaped(text)):
            clear_draft()
            return None
        if not is_rule_shaped(text):
            return draft["question"]
        clear_draft()
    author = is_author_request(text)
    rule_text = text if is_rule_shaped(text) else None
    if rule_text is None:
        return ASK_FOR_RULE if author else None
    rules = parse_rules(rule_text)
    if not rules:
        rules = fill_with_model(rule_text, llm)
    if not rules:
        return ("I can make that, but I couldn't tell when it should happen. "
                "Tell me the time, sunset or sunrise, or the sensor to watch "
                "(like motion or a door).")
    if states is None:
        return ("I can't read Home Assistant right now, so I can't check which "
                "lights you mean. Try again in a moment.")
    index = _states_index(states)
    items = [resolve_rule(spec, index) for spec in rules]
    for res in items:
        if res.question:
            if res.options:
                _set_draft("sensor", items, res.question, conversation_id)
                with _LOCK:
                    if _draft is not None:
                        _draft["options"] = res.options
            return res.question
    return _continue(items, states, conversation_id)


__all__ = [
    "ACTION_DOMAINS", "TRIGGER_TYPES", "is_author_request", "is_rule_shaped",
    "claims_turn", "parse_rules", "validate_spec", "fill_with_model",
    "resolve_rule", "summary", "build_payload", "alias_and_object_id",
    "find_duplicates", "handle_turn", "pending_draft", "clear_draft",
]
