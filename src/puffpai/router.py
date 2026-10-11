"""
PuffPAI Unified Message Router

Centralizes keyword-based message routing that was previously duplicated
in app_ui.py and api_server.py.

Routes user messages to the appropriate handler:
  - AGENT: Messages needing tool use (HA, security, web search, etc.)
  - LIBRARY: Direct knowledge base queries
  - RAG: General knowledge questions (RAG + LLM)

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import re
from enum import Enum


class RouteTarget(Enum):
    """Where a message should be routed."""
    AGENT = "agent"         # Needs tool-calling agent (LangGraph)
    LIBRARY = "library"     # Direct library/collection lookup
    RAG = "rag"             # RAG retrieval + LLM response
    HOME = "home"           # Home Assistant specific (subset of AGENT)
    BRIEFING = "briefing"   # Daily briefing / catch-up request
    APP_LAUNCH = "app_launch"  # Direct app launch (trivia, calendar, etc.)


# ── Keyword definitions ─────────────────────────────────────────────────────

# Daily briefing / catch-up
_BRIEFING_KEYWORDS = [
    'daily update', 'daily briefing', 'daily report',
    'morning briefing', 'morning update', 'morning report',
    'give me my update', 'give me my briefing',
    'what did i miss', "what's going on today",
    "what is going on today", 'catch me up',
    'brief me', 'news briefing', 'daily digest',
    "today's briefing", "today's update",
]

# Home Assistant / smart home control
_HOME_KEYWORDS = [
    'home assistant', 'smart home', 'turn on', 'turn off',
    'thermostat', 'apple tv', 'set brightness', 'dim the',
    'the light', 'the lights', 'the switch', 'the fan',
    'discover devices', 'list devices', 'control the',
    'christmas lights', 'holiday lights', 'activate scene',
    # Hardware / Zigbee onboarding (USB hardware, not IP network)
    'scan for new devices', 'scan for new hardware',
    'look for new hardware', 'detect my dongle', 'detect my device',
    'i just plugged', 'i plugged in', 'new dongle', 'usb dongle',
    'zigbee', 'zigbee dongle', 'zigbee coordinator', 'z-wave',
    'pair zigbee', 'pair my zigbee', 'add zigbee',
    'zigbee manager', 'pairing mode', 'permit join',
    'manage it', 'manage this device', 'add as managed',
    'add it to home assistant', 'add this to home assistant',
    # Cameras / dashboard tiles (Reolink etc.)
    'camera', 'cameras', 'reolink', 'nvr', 'home hub',
    'my dashboard', 'the dashboard', 'to my dashboard',
    'add it to the dashboard', 'on the dashboard',
    # Modular dashboard widgets
    'widget', 'widgets', 'news feed', 'rss',
]

# Privacy / network security
_SECURITY_KEYWORDS = [
    'block device', 'unblock device', 'privacy scan',
    'lockdown mode', 'firewall', 'internet access',
    'security status', 'needs approval', 'approval',
    'network scan', 'scan my network', 'scan the network',
    'scan of the network', 'do a scan', 'run a scan',
    'who is on my network', 'devices on my network',
    'devices connected to my network',
    # Zone 3 — Network Edge
    'zone 3', 'zone3', 'smart device', 'secure my device',
    'secure my network', 'discover device', 'telemetry',
    'dns sinkhole', 'pihole', 'pi-hole', 'device security',
    'manage my device', 'what devices are on',
    'block ip', 'block that device', 'network security',
    # Sentinel consent-request loop
    'approve', 'consent', 'permission', 'authorize',
    'go ahead', 'allow it', 'deny it',
]

# Emergency preparedness
_PREP_KEYWORDS = [
    'emergency supplies', 'preparedness', 'emergency inventory',
    'water supply', 'food supply', 'weather alerts',
    'expiring supplies', 'expiration date',
    'supply recommend', 'prep recommend', 'family size',
    # Benchmark 2.7: quantity questions ("How many days of food do we
    # have?") matched nothing here and fell through to RAG, which has no
    # tools — prep_days_remaining could never fire.
    'days of food', 'days of water', 'days of supplies',
    'how much food', 'how much water', 'enough food', 'enough water',
    'food do we have', 'water do we have', 'rations',
]

# Web search triggers (phrases to avoid false positives)
_WEB_SEARCH_KEYWORDS = [
    'search for', 'search the', 'look up', 'google',
    'find out', 'latest news', 'current news',
    'weather', 'forecast', 'news', 'headlines',
    'what is happening', "what's happening",
    'right now', 'this week', 'this year',
    'price of', 'cost of', 'how much is', 'how much does',
    'stock price', 'stock market', 'market today',
    'who won', 'who is winning', 'game score',
    'search the web', 'search online',
]

# Media server (Jellyfin / Calibre-Web)
_MEDIA_KEYWORDS = [
    'media server', 'jellyfin', 'movie', 'movies', 'film', 'films',
    'music library', 'my music', ' music', 'song', 'songs', 'album', 'albums',
    'artist', 'audiobook', 'audiobooks', 'ebook', 'ebooks',
    'playlist', 'playlists', 'shuffle',
    'now playing', 'currently playing', "what's playing", 'what is playing',
    'pause the', 'resume the', 'skip this', 'next song', 'next track',
    'play music', 'play movie', 'play video', 'play some', 'watch',
    'what movies', 'what music', 'what books', 'my library',
    'media library', 'search movies', 'search music', 'search books',
    'genre', 'genres', 'collection of movies', 'collection of music',
]

# Action keywords that need tool access
_ACTION_KEYWORDS = [
    'calculate', 'compute', 'multiply', 'divide',
    ' times ', ' plus ', ' minus ', 'divided by', 'sum of',
    'add up', 'subtract', 'percentage of', 'percent of',
    'create file', 'write file', 'save to', 'write to',
    'read file', 'open file', 'show file',
    'add to checklist', 'save to checklist', 'prep list',
]

# App-specific keywords (AlarmFlow, Calendar, FreshTable, FitFam, Trivia)
_APP_KEYWORDS = [
    # AlarmFlow
    'timer', 'alarm', 'countdown', 'wake me up', 'remind me in',
    'remind me to', 'set a timer', 'set an alarm', 'stop timer', 'cancel alarm',
    # Calendar
    'calendar', 'schedule', 'event', 'appointment', 'meeting',
    'task', 'to do', 'todo', 'medication', 'medicine', 'bill',
    'add to calendar', 'what is on my schedule',
    # FreshTable
    'pantry', 'shopping list', 'grocery', 'running low',
    'meal plan', 'add to pantry', 'add to shopping',
    # FitFam
    'workout', 'exercise', 'fitness', 'streak',
    'log workout', 'fitness status',
    # Trivia
    'trivia', 'quiz', 'trivia stats',
]

# Personal/uploaded document keywords — route to AGENT so the LLM can
# call search_my_documents instead of hallucinating from training data.
_DOCUMENT_KEYWORDS = [
    # Personal possessives
    "my recipe", "my recipes", "from my mom", "from my dad",
    "from my grandma", "from my grandpa", "from my mother",
    "from my father", "from my family",
    "from grandma", "from grandpa", "from mom", "from dad",
    "from mother", "from father",
    "i uploaded", "i sent you", "i added",
    "my document", "my documents", "my file", "my files",
    "my notes", "my note", "my pdf", "my photo",
    "my study", "my homework", "my textbook",
    "my history class", "my biology class", "my school",
    "my flyer", "the flyer", "the recipe", "that recipe",
    "the schedule i", "my excel", "my spreadsheet",
    "uploaded document", "uploaded file", "uploaded photo",
    "in my upload",
    # Recipe/ingredient phrases that usually refer to a user's collection
    "recipe for", "recipe from", "ingredients for", "ingredients in",
    "what was the recipe", "what was that recipe", "the chicken recipe",
    "what's in the recipe", "what is the recipe", "show me the recipe",
    # Loose recipe-noun matches — anything mentioning "recipe" should
    # check the user's collection first before falling back to RAG.
    "recipe", "recipes",
    # Common "give me / show me / pull up / find" + content phrasing
    "give me the", "give me my", "show me the", "show me my",
    "pull up the", "pull up my", "find the", "find my",
    "tell me about my", "tell me what's in my", "open the",
]

# Library/collection queries
_LIBRARY_KEYWORDS = [
    'librar', 'database', 'indexed', 'collection',
    'kiwix', 'wikipedia', 'knowledge base',
]

_LIBRARY_QUERY_WORDS = [
    'list', 'how many', 'what', 'which', 'show', 'tell',
    'about', 'describe', 'do you have', 'do you know',
    'available', 'access', 'can you search',
]

# All agent-triggering keywords combined
_ALL_AGENT_KEYWORDS = (
    _HOME_KEYWORDS + _SECURITY_KEYWORDS + _PREP_KEYWORDS +
    _WEB_SEARCH_KEYWORDS + _APP_KEYWORDS +
    _MEDIA_KEYWORDS + _DOCUMENT_KEYWORDS
)


# ── Action-intent safety net ────────────────────────────────────────────────
# The RAG path has NO tools: anything action-shaped that slips through the
# keyword lists lands there and the LLM tends to *claim* it acted ("Sure,
# I'll turn on the lights") without doing anything. Catch imperative
# verb+object phrasing and comfort/indirect requests and keep them on the
# AGENT path, which can actually execute (or honestly decline).

_ACTION_VERBS_RE = re.compile(
    r"\b(?:turn|switch|dim|brighten|set|start|stop|pause|resume|play|skip|"
    r"add|create|make|schedule|remind|cancel|delete|remove|lock|unlock|"
    r"open|close|adjust|lower|raise|activate|deactivate|launch|queue)\b")

_ACTION_OBJECTS_RE = re.compile(
    r"\b(?:light|lights|lamp|fan|thermostat|air\s*condition\w*|temperature|temp|heat|ac\b|tv|"
    r"timer|alarm|reminder|task|event|appointment|meeting|list|"
    r"music|song|playlist|movie|device|door|camera|scene|volume|"
    r"grocery|groceries|shopping)\b")

_COMFORT_RE = re.compile(
    r"\btoo (?:dark|bright|hot|cold|warm|loud|quiet)\b"
    r"|\b(?:it'?s|its) (?:freezing|boiling|dark|chilly) in\b"
    r"|\bmake sure i (?:don'?t|do not) forget\b"
    r"|\bdon'?t let me forget\b"
    r"|\bwake me\b")


def _looks_like_action_request(lower: str) -> bool:
    """True when the message is a do-something request in indirect words."""
    if _COMFORT_RE.search(lower):
        return True
    return bool(_ACTION_VERBS_RE.search(lower)
                and _ACTION_OBJECTS_RE.search(lower))


# ── House-STATE questions ───────────────────────────────────────────────────
# Reading the house was the hole in the keyword lists: every *command* had a
# keyword ("turn on", "thermostat"), but the matching *question* did not.
# "What is the temperature in the living room?" and "is the porch light on?"
# both fell through to RAG, which has no tools and so answered — falsely —
# that it cannot read the house. Both entities are live and the Concierge
# reads them correctly once the message reaches the team.
#
# The discriminator is THIS HOUSE vs THE WORLD, expressed as three explicit,
# testable signals; no model call, because this runs on every message:
#   1. question SHAPE — state-shaped ("is the X on", "what's the status of")
#      or reading-shaped ("what's the temperature", "how warm is it");
#   2. a HOUSE NOUN (device) or a HOUSE AREA (room) to anchor it here;
#   3. a WORLD veto — averages, months/seasons, physics, other places — which
#      leaves the message exactly where it is today (RAG).
# Rule 2 is what keeps "what is the average temperature in Texas in August"
# on the knowledge path: it has the word "temperature" and nothing else.

_HOUSE_DEVICE_RE = re.compile(
    r"\b(?:thermostat|thermostats|light|lights|lamp|lamps|bulb|bulbs|"
    r"switch|switches|outlet|outlets|plug|plugs|dimmer|fan|fans|"
    r"heater|heat pump|furnace|air ?conditioner|camera|cameras|"
    r"lock|locks|door|doors|doorbell|garage|gate|window|windows|"
    r"blind|blinds|shade|shades|curtain|curtains|siren|alarm system|"
    r"speaker|speakers|tv|television|sensor|sensors|motion detector|"
    r"vacuum|humidifier|dehumidifier|air purifier|oven|stove|"
    r"washer|dryer|dishwasher|coffee ?maker|kettle|sprinkler|sprinklers)\b")

_HOUSE_AREA_RE = re.compile(
    r"\b(?:living ?room|family ?room|dining ?room|bed ?room|guest ?room|"
    r"nursery|kitchen|bath ?room|hall ?way|hall|entry ?way|foyer|"
    r"office|study|den|garage|basement|attic|laundry|mud ?room|closet|"
    r"pantry|porch|patio|deck|driveway|back ?yard|front ?yard|yard|"
    r"shed|barn|upstairs|downstairs|front door|back door|side door|room|"
    r"in here|in this room|in the house|around the house|at home)\b")

_STATE_QUESTION_RE = re.compile(
    r"\b(?:is|are|was|were)\s+(?:the|my|our|any|all|both)\b[^.?!]{0,60}"
    r"\b(?:on|off|open|closed|shut|locked|unlocked|running|playing|"
    r"armed|disarmed|active|idle|charging|empty|full|set to)\b"
    r"|\bwhat(?:'s| is| are)\s+(?:the\s+)?(?:state|status|setting|mode)\b"
    r"|\b(?:state|status)\s+of\s+(?:the|my|our)\b"
    r"|\bdid\s+(?:i|we|anyone|someone|somebody)\s+leave\b"
    r"|\bis\s+anything\s+(?:still\s+)?(?:on|open|unlocked|running)\b"
    r"|\bhow\s+many\b[^.?!]{0,40}\bare\s+(?:on|off|open|running)\b")

_HOUSE_READING_RE = re.compile(
    r"\bwhat(?:'s| is)\s+(?:the\s+)?(?:current\s+)?(?:temperature|temp|humidity)\b"
    r"|\bwhat\s+temperature\s+is\s+it\b"
    r"|\bhow\s+(?:warm|cold|hot|humid)\s+is\s+it\b")

#: Signals that a reading question is about the world, not this house. Only
#: ever *removes* a message from the house path, so a miss here is today's
#: behaviour, not a new failure.
_NOT_THIS_HOUSE_RE = re.compile(
    r"\b(?:average|typical|normal|mean|median|record|historical|historically|"
    r"climate|seasonal|global|globally|worldwide|nationwide|planet|earth|"
    r"mars|moon|sun|space|ocean|boil|boils|boiling|freezing point|"
    r"melting point|body temperature|fever|human body|absolute zero|kelvin|"
    r"surface of|weather|forecast)\b"
    r"|\bin\s+(?:january|february|march|april|june|july|august|september|"
    r"october|november|december|summer|winter|spring|autumn)\b"
    r"|\bhow\s+do\s+i\b|\bwhat\s+does\s+the\s+manual\b")

#: "in/at <somewhere>" — used only to decide whether a *bare* reading question
#: names a place. A place that is not a house area means the world.
_LOCATOR_RE = re.compile(r"\b(?:in|at|near|around|over|for)\s+(?:the\s+|a\s+|an\s+)?[a-z]")

# ── Reading the house as an INSTRUCTION, not a question ─────────────────────
# The signals above are all interrogative ("what's the temperature", "is the
# light on"). Ask for the same reading in the imperative — "check the
# temperature in the living room", "read the living room temperature", "list my
# devices" — and every one of them missed, so the message went to RAG, which
# has no tools and answered that it cannot read the house. That hit compound
# requests hardest: "check the temperature in the living room and then tell me
# what devices are here" never reached the Concierge, and so never reached the
# planning loop, even though the Concierge resolves and plans it correctly.

#: What can actually be READ off this house. Deliberately not "any house noun":
#: a room name alone is a topic ("tell me about the history of the kitchen"),
#: not a reading.
_HOUSE_READABLE_RE = re.compile(
    r"\b(?:temperature|temp|humidity|thermostat|sensors?|readings?|"
    r"battery|status|state)\b")

#: Imperative/inspection verbs. "what" is deliberately absent — the question
#: forms are already covered above, and adding it here would re-open
#: "what's the temperature in paris".
_HOUSE_INSPECT_RE = re.compile(
    r"\b(?:check|read|look\s+at|show|list|get|monitor|"
    r"tell\s+me|give\s+me|pull\s+up)\b")

#: The device inventory is the one house reading with no room and no sensor to
#: anchor it, so it gets its own pair: an asking verb plus a possessive/here
#: anchor. The anchor is what keeps "what devices did edison invent" on the
#: knowledge path.
_DEVICE_WORD_RE = re.compile(r"\bdevices?\b")
_DEVICE_ASK_RE = re.compile(
    r"\b(?:what|which|how\s+many|list|show|tell\s+me|give\s+me|discover)\b")
_DEVICE_OURS_RE = re.compile(
    r"\b(?:my|our|here|at\s+home|connected|hooked\s+up|paired)\b"
    r"|\b(?:do|does)\s+(?:i|we)\s+have\b|\bare\s+there\b"
    r"|\b(?:the|this)\s+house\b"
    r"|\b(?:every|all)\s+(?:the\s+|my\s+|our\s+)?devices?\b")


def _looks_like_house_state_question(lower: str) -> bool:
    """True when the message asks about the state of THIS house.

    Deterministic and cheap — no LLM, no I/O. See the block comments above for
    the signals; rule 2 (a house noun or a room) is the one doing the real work,
    and the imperative branch requires a verb AND a readable AND that anchor,
    because any two of the three still describe a knowledge question.
    """
    if _NOT_THIS_HOUSE_RE.search(lower):
        return False

    here = bool(_HOUSE_DEVICE_RE.search(lower) or _HOUSE_AREA_RE.search(lower))

    if _STATE_QUESTION_RE.search(lower) and here:
        return True

    if (_DEVICE_WORD_RE.search(lower) and _DEVICE_ASK_RE.search(lower)
            and (_DEVICE_OURS_RE.search(lower) or here)):
        return True

    if (_HOUSE_INSPECT_RE.search(lower) and _HOUSE_READABLE_RE.search(lower)
            and here):
        return True

    if _HOUSE_READING_RE.search(lower):
        if here:
            return True
        # Ambiguity call, deliberate: a bare "what's the temperature?" with no
        # place at all is treated as a HOUSE question. A house assistant that
        # answers about the house is right more often than not, and the
        # knowledge path stays one rephrase away. A reading question that DOES
        # name a place ("...in Texas") is the world and is left alone.
        return not _LOCATOR_RE.search(lower)

    return False


# ── Planful / workflow-creation phrasing ────────────────────────────────────
# Keyword-free requests to *build* a workflow or routine ("create a workflow
# that greets my guests", "set up my morning routine") matched no keyword list
# and fell through to RAG, which cannot compose or run a multi-step plan — so
# the request came back as prose and never reached the planning loop. This
# detector, consulted only just before the RAG fallback, keeps those on the
# AGENT path. It mirrors the SPIRIT of concierge._MULTI_STEP_RE (planful, do-a-
# sequence phrasing) but is deliberately conservative: it fires only when an
# imperative construction VERB ("create", "build me", "set up", "put together",
# "compose", "organi[sz]e", "plan out"…) is paired with a PLAN OBJECT
# ("workflow", "routine", "sequence", "automation"…). Requiring both keeps
# plain knowledge questions on RAG — "what is the plan for the meeting" (a
# question, no construction verb, no object), "tell me about workflows" (object
# but no verb), "what's the capital of France" (neither).

_PLANFUL_VERB_RE = re.compile(
    r"\b(?:create|build|compose|design|assemble|arrange|"
    r"organi[sz]e|coordinate|orchestrate)\b"
    r"|\bset up\b|\bput together\b|\bplan out\b|\bmap out\b|\blay out\b")

_PLAN_OBJECT_RE = re.compile(
    r"\b(?:workflow|workflows|routine|routines|sequence|sequences|"
    r"automation|automations|multi-?step|"
    r"series of (?:steps|actions|tasks))\b")


def _looks_like_planning_request(lower: str) -> bool:
    """True when the message asks the team to *build* a workflow or routine.

    Conservative by design — an imperative construction verb AND a plan object
    must both be present. Either one alone is a knowledge question and stays on
    RAG. Deterministic and cheap; no LLM, no I/O.
    """
    return bool(_PLANFUL_VERB_RE.search(lower)
                and _PLAN_OBJECT_RE.search(lower))


# ── Zigbee pairing phrasing ─────────────────────────────────────────────────
# "pair my new light bulbs" matched no keyword list, so the pairing assistant's
# resolver rules in the Concierge would be unreachable from chat: the request
# falls to RAG and comes back as LED shopping advice. Verb-form "pair …"
# phrasings, pairing-mode nouns,
# and the status question route AGENT. Word-boundary \b keeps "repair",
# "impair" and "compare" out; the determiner after "pair" keeps "a pair of
# pliers" prose out.

_PAIRING_RE = re.compile(
    r"\bpair\s+(?:my|the|these|those|some|up|new)\b"
    r"|\bpairing\s+(?:mode|status|window)\b"
    r"|\b(?:start|enter|begin|open)\s+pairing\b"
    r"|\bdid\s+you\s+find\s+any\s+new\s+devices?\b")


def _looks_like_pairing_request(lower: str) -> bool:
    """True when the message is about Zigbee pairing or its outcome."""
    return bool(_PAIRING_RE.search(lower))


# ── Router Functions ────────────────────────────────────────────────────────

def route_message(text: str, mode: str = "auto") -> RouteTarget:
    """Determine the best route for a user message.

    Args:
        text: The user's message text.
        mode: Routing mode - "auto" (keyword detection), "home" (force HA),
              "general" (force RAG).

    Returns:
        RouteTarget indicating where to send the message.
    """
    mode = (mode or "auto").strip().lower()

    if mode == "home":
        return RouteTarget.HOME

    if mode == "general":
        return RouteTarget.RAG

    lower = (text or "").lower()

    # Check for daily briefing first (special compound command)
    if any(k in lower for k in _BRIEFING_KEYWORDS):
        return RouteTarget.BRIEFING

    # Check for app launch intent (open/play/launch an app — bypasses LLM).
    # Consulted BEFORE the generic agent/action keyword lists: an app name
    # ("trivia", "calendar", "timer") also lives in _APP_KEYWORDS, so a launch
    # phrasing like "play a trivia game" or "open the calendar" would otherwise
    # match _ALL_AGENT_KEYWORDS first and be shadowed into AGENT, never reaching
    # the direct app launcher. detect_app_launch() is deliberately narrow — it
    # requires an explicit launch verb AND a known app name, and returns None
    # for data operations ("add to the calendar", "set a timer") — so promoting
    # it here cannot steal those; they fall through to the keyword lists below.
    if detect_app_launch(lower) is not None:
        return RouteTarget.APP_LAUNCH

    # Check for agent-requiring keywords BEFORE library check.
    # This prevents "movie library", "music library" etc. from
    # being misrouted to the knowledge-base library handler.
    if any(k in lower for k in _ALL_AGENT_KEYWORDS):
        return RouteTarget.AGENT

    # Check for action keywords (calculator, file ops, etc.)
    if any(k in lower for k in _ACTION_KEYWORDS):
        return RouteTarget.AGENT

    # Check for library queries (direct lookup, no LLM needed)
    # Only matches if no agent keywords were found above.
    if _is_library_query(lower):
        return RouteTarget.LIBRARY

    # Reading the house is as much a tool job as changing it: RAG has no way
    # to see a thermostat, so it denies having one. Checked after the library
    # rule so it does not lose a message to it.
    if _looks_like_house_state_question(lower):
        return RouteTarget.AGENT

    # Action-shaped phrasing that missed every keyword list still needs
    # the tool-capable agent — RAG would hallucinate compliance.
    if _looks_like_action_request(lower):
        return RouteTarget.AGENT

    # Planful / workflow-creation phrasing with no matching keyword still needs
    # the agent team: RAG cannot compose or run a multi-step routine, so a
    # request to *build* one would otherwise fall to RAG and be answered as
    # prose instead of reaching the planning loop. Checked last, just before
    # the RAG fallback, so it only ever rescues messages RAG would have taken.
    if _looks_like_planning_request(lower):
        return RouteTarget.AGENT

    if _looks_like_pairing_request(lower):
        return RouteTarget.AGENT

    # Default: RAG + LLM response
    return RouteTarget.RAG


def should_use_home_tools(text: str, mode: str = "auto") -> bool:
    """Legacy compatibility: returns True if message should use HA tools.

    This is the function that api_server.py was using before. It now
    delegates to route_message().
    """
    route = route_message(text, mode)
    return route in (RouteTarget.AGENT, RouteTarget.HOME, RouteTarget.BRIEFING)


def detect_needs_tools(message: str) -> bool:
    """Check if a message requires tool access (agent mode).

    This replaces app_ui.py's detect_needs_tools method.
    """
    route = route_message(message)
    return route == RouteTarget.AGENT


def get_route_category(text: str) -> str:
    """Return a human-readable category string for logging.

    Returns one of: "home", "security", "prep", "web_search",
    "action", "library", "rag".
    """
    lower = (text or "").lower()

    if any(k in lower for k in _BRIEFING_KEYWORDS):
        return "briefing"
    if any(k in lower for k in _HOME_KEYWORDS):
        return "home"
    if any(k in lower for k in _SECURITY_KEYWORDS):
        return "security"
    if any(k in lower for k in _PREP_KEYWORDS):
        return "prep"
    if any(k in lower for k in _WEB_SEARCH_KEYWORDS):
        return "web_search"
    if any(k in lower for k in _MEDIA_KEYWORDS):
        return "media"
    if any(k in lower for k in _APP_KEYWORDS):
        return "app"
    if any(k in lower for k in _ACTION_KEYWORDS):
        return "action"
    if _is_library_query(lower):
        return "library"
    # Same order as route_message(), so category and route never disagree.
    if _looks_like_house_state_question(lower):
        return "home"
    return "rag"


def _is_library_query(lower_text: str) -> bool:
    """Check if the message is asking about indexed libraries/collections."""
    # Citation-style questions ("which sources did you use", "cite your sources",
    # "what did you reference") are about the PREVIOUS answer, not a request to
    # list the entire indexed library. Keep them on the RAG path so the LLM
    # can answer using conversation context and the [Sources Used For This
    # Answer] block from the prior turn's RAG context.
    citation_phrases = (
        'source', 'sources', 'cite', 'citation', 'citations',
        'reference', 'references', 'pulled from', 'get that from',
        'get this from', 'get that information', 'where did you',
        'come from', 'came from', 'did that answer',
    )
    if any(p in lower_text for p in citation_phrases):
        return False

    # "What does your library say about X?" is a CONTENT question — the
    # user wants an answer from the knowledge base, not a catalog dump.
    # Only treat it as a library-listing request when the message is
    # about the collections themselves, with no content topic attached.
    if re.search(r'\b(?:library|libraries|knowledge base|collections?)\b'
                 r'[^.?!]{0,30}\b(?:say|says|know|tell|cover|contain|have)\b'
                 r'[^.?!]{0,10}\babout\b', lower_text):
        return False

    has_library_word = any(k in lower_text for k in _LIBRARY_KEYWORDS)
    has_query_word = any(k in lower_text for k in _LIBRARY_QUERY_WORDS)
    return has_library_word and has_query_word


# ── App launch intent patterns ────────────────────────────────────────────

# Phrases that mean "open this app" (not "add data to it")
_APP_LAUNCH_VERBS = [
    'play', 'open', 'launch', 'start', 'show me', 'pull up',
    'let me see', 'bring up', "let's do", "i want to play",
    "i'd like to play", 'can we play', 'can i play',
]

# Map from keyword → app_id (used for deterministic app opening)
_APP_NAME_MAP = {
    'trivia': 'trivia', 'quiz': 'trivia', 'trivia game': 'trivia',
    'brain training': 'trivia', 'brain game': 'trivia',
    'a game': 'trivia', 'game about': 'trivia',
    'calendar': 'calendar', 'my calendar': 'calendar',
    'my schedule': 'calendar', 'the schedule': 'calendar',
    'alarm': 'alarm', 'timer app': 'alarm', 'alarmflow': 'alarm',
    'the timer': 'alarm', 'timers': 'alarm',
    'freshtable': 'freshtable', 'pantry app': 'freshtable',
    'shopping app': 'freshtable', 'grocery app': 'freshtable',
    'fitfam': 'fitfam', 'fitness app': 'fitfam',
    'workout app': 'fitfam', 'fitness tracker': 'fitfam',
}


def detect_app_launch(text: str) -> str | None:
    """Detect if the user wants to open/launch an app.

    Returns the app_id (e.g. 'trivia', 'calendar') if a launch intent is
    detected, or None if this is a data operation (add, list, etc.).
    """
    lower = (text or "").lower()

    # Must have a launch verb
    has_launch_verb = any(v in lower for v in _APP_LAUNCH_VERBS)
    if not has_launch_verb:
        return None

    # Check for app names
    for keyword, app_id in _APP_NAME_MAP.items():
        if keyword in lower:
            return app_id

    return None
