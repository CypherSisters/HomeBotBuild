"""A thermostat set is confirmed by reading each thermostat back. One owner.

Without it, "set the living room and bedroom to 74" can be answered "The
thermostat in the living room and bedroom is now set to 74." while the
thermostats read living room 74, **bedroom 73**. Two defects, one class,
the same class :mod:`puffpai.agents.shopping_writes` closes:

* the turn resolves ONE entity (the Concierge's ``_delegate_to_steward``
  resolves a single ``entity_id``) and the second target is never called; and
* the confirmation is prose about the REQUEST, so it names both rooms whether
  or not either call landed.

This module owns the whole shape for every surface: resolve N targets, call the
service PER TARGET, read each one back, and compose the sentence from what the
thermostats say now::

    Living room set to 74. Bedroom is still at 73 — it didn't take;
    want me to try again?

Follow-up context lives here too: "what's the temperature in the living
room" → "set it to 74" must mean the living room. The room is recovered from
the previous user turns of the same conversation, which is where it was
actually said, and only when the new command names no room of its own.
"""

from __future__ import annotations

import re
import time

from puffpai.logging_config import get_logger

logger = get_logger(__name__)

#: Longest utterance the fastpath will claim.
MAX_LEN = 160

#: Setpoints outside this band are not a household thermostat command. The
#: bridge would happily forward 740; this declines instead of guessing.
MIN_TEMP, MAX_TEMP = 45, 95

#: How far back a carried room may come from: the previous two user turns.
CONTEXT_TURNS = 2

_SET_VERB_RE = re.compile(
    r"(?i)\b(?:set|turn|make|put|change|adjust|bump|drop|raise|lower|crank)\b")

#: The command is about something else that also takes a number.
_BLOCK_RE = re.compile(
    r"(?i)\b(?:volume|louder|quieter|timer|timers|alarm|alarms|minutes?|"
    r"seconds?|hours?|percent|%|brightness|dim|channel|station|song|track|"
    r"reminder|list|recipe)\b")

_CLIMATE_NOUN_RE = re.compile(
    r"(?i)\b(?:thermostat|thermostats|temperature|temp|heat|heating|"
    r"cooling|a/?c|air\s+conditioning|degrees?)\b")

#: "set it to 74" / "set that to 74" — a command that points at the last thing
#: talked about instead of naming a room.
_PRONOUN_RE = re.compile(r"(?i)\b(?:it|that|those|them|there)\b")

_NUMBER_WORDS = {
    "sixty": 60, "sixty-one": 61, "sixty-two": 62, "sixty-three": 63,
    "sixty-four": 64, "sixty-five": 65, "sixty-six": 66, "sixty-seven": 67,
    "sixty-eight": 68, "sixty-nine": 69, "seventy": 70, "seventy-one": 71,
    "seventy-two": 72, "seventy-three": 73, "seventy-four": 74,
    "seventy-five": 75, "seventy-six": 76, "seventy-seven": 77,
    "seventy-eight": 78, "seventy-nine": 79, "eighty": 80,
}


def _spoken_number(text: str):
    """"seventy four" / "seventy-four" → 74. Only the thermostat band."""
    low = re.sub(r"\s+", " ", str(text or "").lower())
    # Longest first: "seventy four" must not be read as "seventy".
    for word, value in sorted(_NUMBER_WORDS.items(),
                              key=lambda kv: -len(kv[0])):
        spaced = word.replace("-", " ")
        if re.search(r"\b" + re.escape(spaced) + r"\b", low):
            return value
    return None


def parse_temperature(text: str):
    """The setpoint this utterance names, or None. Pure."""
    low = str(text or "")
    for match in re.finditer(r"\b(\d{2,3})(?:\s*(?:°|degrees?|deg))?\b", low):
        value = int(match.group(1))
        if MIN_TEMP <= value <= MAX_TEMP:
            return value
    return _spoken_number(low)


# ── entities ────────────────────────────────────────────────────────

def list_thermostats(bridge) -> list:
    """``[{entity_id, name, current, setpoint}]`` for every ``climate.*``."""
    try:
        states = bridge.get_all_states() or []
    except Exception as exc:
        logger.debug("climate entity list failed: %s", exc)
        return []
    out = []
    for raw in states:
        entity_id = str((raw or {}).get("entity_id") or "")
        if not entity_id.startswith("climate."):
            continue
        attrs = (raw.get("attributes") or {})
        out.append({
            "entity_id": entity_id,
            "name": str(attrs.get("friendly_name")
                        or entity_id.split(".", 1)[1].replace("_", " ")),
            "setpoint": attrs.get("temperature"),
            "current": attrs.get("current_temperature"),
            "area": str(attrs.get("area") or attrs.get("area_name")
                        or attrs.get("area_id") or ""),
        })
    return out


def _name_tokens(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def _mentions(text: str, name: str) -> bool:
    tokens = _name_tokens(name)
    if not tokens:
        return False
    # "Living Room Thermostat" is named by "living room"; the noun the entity
    # carries is not part of what the household says.
    tokens = re.sub(r"\b(?:thermostat|climate|hvac|ac|heater)\b", " ",
                    tokens).strip()
    if not tokens:
        return False
    return bool(re.search(r"\b" + re.escape(tokens) + r"\b",
                          _name_tokens(text)))


#: Rooms a household says out loud. The friendly name a thermostat carries is
#: rarely what anyone calls the room — this box's second unit is "Bedroom
#: Hallway Bedroom" and nobody says that — so a spoken room is matched by
#: TOKEN CONTAINMENT against the entity name, not as a whole phrase (T1c).
ROOM_PHRASES = (
    "living room", "family room", "dining room", "guest room",
    "guest bedroom", "master bedroom", "main bedroom", "spare room",
    "kids room", "kid's room", "rec room", "game room", "laundry room",
    "bonus room", "sun room", "front room", "back room", "media room",
    "great room", "bedroom", "kitchen", "office", "den", "garage",
    "basement", "attic", "loft", "nursery", "bathroom", "hallway", "hall",
    "upstairs", "downstairs", "playroom", "study", "porch", "patio",
    "gym", "studio", "shop", "barn", "cabin", "lounge", "nook",
)

#: Tokens of an entity name that identify no room on their own.
_GENERIC_TOKENS = frozenset({
    "room", "thermostat", "climate", "hvac", "ac", "heater", "unit", "zone",
    "floor", "main", "my", "the", "home", "house", "puffpai", "stat", "air",
})


def _stripped_name(name: str) -> str:
    """The entity's name with the appliance noun removed."""
    return re.sub(r"\b(?:thermostat|climate|hvac|ac|heater)\b", " ",
                  _name_tokens(name)).strip()


def _entity_token_set(entry) -> frozenset:
    return frozenset(_stripped_name(entry.get("name")).split())


def _candidate_phrases(thermostats) -> list:
    """Room phrases worth looking for: the lexicon, plus each entity's own
    distinguishing tokens (a name like "Loft North" is still findable)."""
    phrases = set(ROOM_PHRASES)
    for entry in thermostats:
        for token in _entity_token_set(entry):
            if len(token) > 2 and token not in _GENERIC_TOKENS:
                phrases.add(token)
    # Longest first: "living room" must be claimed before "room".
    return sorted(phrases, key=lambda p: (-len(p.split()), -len(p), p))


def _area_matches(entry, phrase_tokens) -> bool:
    area = frozenset(_name_tokens(entry.get("area")).split())
    return bool(area) and phrase_tokens <= area


def resolve_targets(text: str, thermostats) -> dict:
    """Resolve every room this utterance names.

    ``{"targets": [entry, ...], "missing": ["bedroom"],
       "ambiguous": [("bedroom", ["Guest Bedroom", "Master Bedroom"])]}``

    A named room NEVER disappears: it lands in exactly one of the three lists
    so the reply can account for it.
    """
    low = _name_tokens(text)
    hits = []           # (position, entry)
    claimed = []        # spans of `low` already spoken for
    missing = []
    ambiguous = []
    seen = set()

    def _overlaps(start, end):
        return any(start < c_end and c_start < end for c_start, c_end in claimed)

    def _take(position, entry):
        if entry["entity_id"] in seen:
            return
        seen.add(entry["entity_id"])
        hits.append((position, entry))

    # Pass 1 — the entity's full name, said as a phrase. An exact naming wins
    # over any containment match and takes its span off the table.
    for entry in thermostats:
        stripped = _stripped_name(entry.get("name"))
        if not stripped:
            continue
        match = re.search(r"\b" + re.escape(stripped) + r"\b", low)
        if match:
            claimed.append(match.span())
            _take(match.start(), entry)

    # Pass 2 — a spoken room, matched by token containment.
    for phrase in _candidate_phrases(thermostats):
        match = re.search(r"\b" + re.escape(phrase) + r"\b", low)
        if not match or _overlaps(*match.span()):
            continue
        claimed.append(match.span())
        tokens = frozenset(phrase.split())
        candidates = [e for e in thermostats
                      if tokens <= _entity_token_set(e)]
        if len(candidates) > 1:
            narrowed = [e for e in candidates if _area_matches(e, tokens)]
            if len(narrowed) == 1:
                candidates = narrowed
        if len(candidates) == 1:
            _take(match.start(), candidates[0])
        elif candidates:
            if not any(e["entity_id"] in seen for e in candidates):
                ambiguous.append(
                    (phrase, sorted(str(e["name"]) for e in candidates)))
        elif phrase in ROOM_PHRASES:
            # A real room word that no thermostat answers to. Only lexicon
            # rooms count as misses; a token borrowed from another entity's
            # name is not something the owner claimed to have.
            missing.append(phrase)

    hits.sort(key=lambda pair: pair[0])
    return {"targets": [entry for _pos, entry in hits],
            "missing": missing, "ambiguous": ambiguous}


def named_targets(text: str, thermostats) -> list:
    """Every thermostat this utterance names, in the order it names them."""
    return resolve_targets(text, thermostats)["targets"]


def carried_targets(history, thermostats) -> list:
    """The room the PREVIOUS turns named, for a command that names none.

    "what's the temperature in the living room" → "set it to 74". The
    previous user turn is where the household said "living room"; the newest turn
    that names exactly one thermostat wins, and a turn that named several is
    not carried (there is nothing unambiguous to carry).
    """
    turns = []
    for entry in (history or []):
        if isinstance(entry, dict):
            turns.append(str(entry.get("user") or entry.get("content") or ""))
        elif isinstance(entry, str):
            turns.append(entry)
    for text in reversed(turns[-CONTEXT_TURNS:]):
        hits = named_targets(text, thermostats)
        if len(hits) == 1:
            return hits
    return remembered_targets(thermostats)


def remembered_targets(thermostats) -> list:
    """The room the last READ answered about, when no history was carried.

    The same follow-up, on the surface that actually speaks (``/api/chat``):
    builds ``conversation_history`` from the request body, and the voice loop
    sends one message per turn — so the previous turn's words are not there to
    read, however the same the conversation id is. The read records the room in
    :mod:`puffpai.turn_context` instead; this borrows it, and only when it
    resolves to exactly one thermostat.
    """
    try:
        from puffpai import turn_context
        room = turn_context.last_room()
    except Exception as exc:
        logger.debug("climate room memory unavailable: %s", exc)
        return []
    if not room:
        return []
    hits = named_targets(room, thermostats)
    return hits if len(hits) == 1 else []


# ── the write, and its read-back ────────────────────────────────────

def set_verified(bridge, entity_id: str, temperature: float,
                 settle: float = 0.4) -> dict:
    """Call the service for ONE thermostat, then read that thermostat back.

    Returns evidence, never a claim::

        {entity_id, name, requested, written, observed, verified, error}

    ``observed`` is the setpoint the entity reports afterwards. One short retry
    while it settles — HA's state machine lags a service call — and then the
    number is reported as it stands, right or wrong.
    """
    result = {"entity_id": entity_id, "name": entity_id,
              "requested": temperature, "written": False,
              "observed": None, "verified": False, "error": None}
    try:
        bridge.set_climate(entity_id, temperature=temperature)
        result["written"] = True
    except Exception as exc:
        result["error"] = str(exc) or exc.__class__.__name__
        logger.warning("climate write failed for %s: %s", entity_id, exc)

    for attempt in (0, 1):
        try:
            raw = bridge.get_entity_state(entity_id) or {}
        except Exception as exc:
            result["error"] = result["error"] or str(exc)
            logger.debug("climate read-back failed for %s: %s", entity_id, exc)
            return result
        attrs = raw.get("attributes") or {}
        result["name"] = str(attrs.get("friendly_name") or result["name"])
        observed = attrs.get("temperature")
        result["observed"] = observed
        try:
            result["verified"] = abs(float(observed) - float(temperature)) < 0.5
        except (TypeError, ValueError):
            result["verified"] = False
        if result["verified"] or attempt:
            break
        time.sleep(settle)
    if not result["verified"] and not result["error"]:
        result["error"] = "setpoint did not change on read-back"
    return result


def _number(value) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def reply_for(results, temperature) -> str:
    """The sentence the read-backs support. One owner, every surface."""
    if not results:
        return "I couldn't find a thermostat to set."
    parts = []
    failed = False
    for item in results:
        name = str(item.get("name") or item.get("entity_id") or "that thermostat")
        if item.get("verified"):
            parts.append(f"{name} set to {_number(temperature)}.")
            continue
        failed = True
        observed = item.get("observed")
        if observed is None:
            parts.append(f"{name} didn't take — I can't read a setpoint back "
                         f"from it.")
        else:
            parts.append(f"{name} is still at {_number(observed)} — "
                         f"it didn't take.")
    sentence = " ".join(parts)
    if failed:
        sentence += " Want me to try again?"
    return sentence


def _missing_sentence(missing) -> str:
    """A named room that resolved to nothing is SAID, never dropped (T1c)."""
    parts = ["I couldn't find a thermostat for '" + phrase + "'."
             for phrase in missing]
    return " ".join(parts)


def handle(user_message, bridge, history=None) -> "str | None":
    """The whole fastpath: parse, resolve N targets, set each, read each back.

    ``None`` means "not a thermostat set" and the turn keeps its route. A
    question with no room it can resolve is ASKED BACK rather than guessed.
    """
    text = str(user_message or "").strip()
    if not text or len(text) > MAX_LEN:
        return None
    if _BLOCK_RE.search(text) or not _SET_VERB_RE.search(text):
        return None
    temperature = parse_temperature(text)
    if temperature is None:
        return None

    thermostats = list_thermostats(bridge)
    if not thermostats:
        return None
    resolved = resolve_targets(text, thermostats)
    targets = resolved["targets"]
    # A room the owner named that matches two thermostats is asked about, not
    # guessed at — even when another room in the same sentence did resolve.
    for phrase, names in resolved["ambiguous"]:
        return ("Which thermostat do you mean for '" + phrase + "': "
                + ", ".join(names) + "?")
    if not targets and resolved["missing"]:
        return _missing_sentence(resolved["missing"])
    if not targets:
        # Only a command that is ABOUT the climate system, or one that points
        # at the previous turn with a pronoun, may borrow a room.
        if not (_CLIMATE_NOUN_RE.search(text) or _PRONOUN_RE.search(text)):
            return None
        targets = carried_targets(history, thermostats)
    if not targets:
        if len(thermostats) == 1:
            targets = list(thermostats)
        else:
            names = sorted(t["name"] for t in thermostats)
            return ("Which thermostat do you mean: "
                    + ", ".join(names) + "?")

    results = []
    for entry in targets:
        result = set_verified(bridge, entry["entity_id"], temperature)
        result.setdefault("name", entry["name"])
        if result.get("name") == result.get("entity_id"):
            result["name"] = entry["name"]
        results.append(result)
    sentence = reply_for(results, temperature)
    if resolved["missing"]:
        sentence += " " + _missing_sentence(resolved["missing"])
    return sentence
