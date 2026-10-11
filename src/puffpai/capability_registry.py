"""
PuffPAI Capability Registry — the single source of truth for what PuffPAI can
do, which tool OWNS each capability, where its data comes from, which surfaces
(voice, chat, dashboard, app pages) expose it, and which words route to it.

WHY THIS EXISTS — the "four weather paths" disease
--------------------------------------------------
Without a registry, capabilities are declared implicitly in four places that
do not know about each other: the voice intent filter
(``AssistantTools._filter_tools_by_intent``), the agent descriptions, the
dashboard endpoints, and the status docs. The dashboard, the voice tools, and
the agents can then each answer the same question from a different source and
disagree, e.g. "any weather alerts?" answered "none" from the forecast while
a real NWS Heat Advisory is live, because the alert authority
(``prep_weather_alerts``) is never consulted.

This module is the ONE place every surface reads, so that:
  * every capability names exactly ONE **authority** tool (kills duplicate
    sources — the dashboard tile and the voice answer resolve to the same
    owner);
  * PuffPAI can **introspect** — "what can you access?", "do you know X?" —
    from declared fact, not a guess;
  * the LLM can **route** — the words a user says map, through declared
    ``keywords``, to the tools that OWN that capability (see
    :func:`tools_for_query`), instead of a 250-line hand-maintained keyword
    wall living off to the side in the tool engine;
  * the answer stays **honest** — :func:`validate` checks every declared tool
    against the live tool set, so a renamed/removed tool trips a test instead
    of silently lying to the user.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

# ── access kinds ─────────────────────────────────────────────────────────────
READ = "read"
WRITE = "write"
READWRITE = "read-write"

# ── status: honest current state of a capability ─────────────────────────────
LIVE = "live"            # works now, verified reachable
NEEDS_AUTH = "needs-auth"  # built, but needs a one-time owner action first
OFFLINE = "offline"      # depends on hardware/a service that is currently down
PLANNED = "planned"      # not built yet — declared so introspection is honest

_STATUS_NOTE = {
    LIVE: "",
    NEEDS_AUTH: " (needs a one-time setup first)",
    OFFLINE: " (currently offline)",
    PLANNED: " (not built yet)",
}

# ── surfaces: ONE capability, many facets — the LLM is the seam ──────────────
# Product doctrine: the agents, the apps, the dashboard, and
# the pages are NOT separate abilities. They are surfaces of ONE capability, and
# the LLM orchestrates across them into a single seamless experience. Because a
# capability names its authority tool exactly ONCE, whichever surface the user
# touches — says it out loud, taps a dashboard tile, opens the app page, or lets
# an agent handle it — resolves to the SAME source and cannot disagree.
VOICE = "voice"          # spoken
CHAT = "chat"            # dashboard chat / typed
DASHBOARD = "dashboard"  # a dashboard tile
APP = "app"              # a full app page (Calendar, FreshTable, AlarmFlow, …)

_SURFACE_PHRASE = {
    VOICE: "just ask me",
    CHAT: "type it in chat",
    DASHBOARD: "see it on your dashboard",
    APP: "open the app",
}


@dataclass(frozen=True)
class Capability:
    """One declared capability. ``authority_tool`` is the single owner — the
    tool that is THE source of truth for this capability's answer. ``keywords``
    are the trigger words that route a user's question to this capability's
    tools (see :func:`tools_for_query`)."""

    id: str                       # stable id, e.g. "weather.alerts"
    domain: str                   # coarse grouping, e.g. "weather"
    label: str                    # human phrase, e.g. "Severe weather alerts"
    authority_tool: str           # THE owning tool ("(none)" if not built yet)
    data_source: str              # where the answer really comes from
    access: str = READ            # READ / WRITE / READWRITE
    also_tools: tuple = ()         # related tools in the same capability
    keywords: tuple = ()           # trigger words → routes here (substring match)
    agent: str | None = None      # owning agent (roster id), if any
    surfaces: tuple = ("voice", "chat")   # where it is exposed
    phrases: tuple = ()            # representative user phrasings (also match)
    #: Phrasings that UNAMBIGUOUSLY mean "answer this now with my read tool".
    #: Distinct from ``keywords``/``phrases``, which are a BINDING signal — they
    #: decide which tools to offer the model, where one tool too many costs
    #: nothing. Dispatch is authoritative: it answers and stops, so a wrong
    #: match has no recovery path. Reusing the binding signal here is what made
    #: "is the front door locked" read out the vault inventory.
    #:
    #: The bar for adding one: could a person say this and mean anything other
    #: than "run this read now"? If yes, it is not a dispatch phrase. So
    #: "network status" qualifies and "network" never does.
    #:
    #: Empty by default, and empty means NOT auto-dispatchable — a capability
    #: added next year cannot become invocable by accident.
    dispatch_phrases: tuple = ()
    status: str = LIVE
    note: str = ""
    always: bool = False          # always bound (the knowledge fallback)
    #: Where a row came from when it is NOT one of the declared ones below.
    #: Empty for every capability in :data:`CAPABILITIES`; ``"skill:<id>"`` for
    #: a row contributed by an installed skill. Read by the dashboard (so a
    #: household can tell an appliance capability from one it approved last
    #: Tuesday) and by the skill installer (so re-validating a skill's triggers
    #: does not find the skill itself).
    origin: str = ""

    @property
    def all_tools(self) -> tuple:
        seen: list = []
        for t in (self.authority_tool, *self.also_tools):
            if t and t != "(none)" and t not in seen:
                seen.append(t)
        return tuple(seen)


# ── human-readable domain order + spoken blurbs ──────────────────────────────
DOMAIN_ORDER = (
    "introspection", "weather", "home", "cameras", "dashboard", "calendar",
    "knowledge", "media", "streaming", "food", "health", "bills", "prep",
    "fitness", "pantry", "security", "vault", "briefing", "news", "agents",
    "system", "utility", "dev", "learning", "maps",
)

#: Short area names for the spoken overview. Only domains listed here appear in
#: the "what can you do" overview (keeps it inside one ~500-char voice turn);
#: niche/utility domains are still fully introspectable via "what can you do
#: with my <area>".
_DOMAIN_NICE = {
    "weather": "weather and alerts",
    "home": "home devices",
    "cameras": "cameras",
    "calendar": "calendar and reminders",
    "knowledge": "knowledge and documents",
    "media": "media",
    "streaming": "streaming",
    "food": "recipes and meals",
    "health": "meds",
    "bills": "bills",
    "prep": "emergency prep",
    "fitness": "fitness",
    "pantry": "pantry and shopping",
    "security": "network security",
    "vault": "your vault",
    "news": "news",
    "agents": "my agent team",
}

DOMAIN_BLURB = {
    "introspection": "my own capabilities — what I can reach and do",
    "weather": "current conditions and severe weather alerts",
    "home": "your Home Assistant devices — status, control, and pairing",
    "cameras": "recent motion and visitor alerts from your cameras",
    "dashboard": "the tiles and widgets on your dashboard",
    "calendar": "tasks, reminders, events, timers, and alarms",
    "knowledge": "your offline library and the documents you've uploaded",
    "media": "your Jellyfin music, movies, and books",
    "streaming": "Spotify and World Radio",
    "food": "recipes, meal plans, and grocery orders",
    "health": "logging and tracking your medications",
    "bills": "your bills — adding them and marking them paid",
    "prep": "your emergency supplies and days-of-food",
    "fitness": "your workouts, personal records, and goals",
    "pantry": "your pantry and shopping list",
    "security": "your network — scanning, blocking devices, and lockdown",
    "vault": "your private vault, unlocked with a spoken PIN",
    "briefing": "your daily briefing",
    "news": "today's news headlines",
    "agents": "my on-device agent team and tasking them",
    "system": "checking PuffPAI's own health",
    "utility": "a calculator, sandbox files, and web search",
    "dev": "writing code — scripts, functions, automations and flows",
    "learning": "trivia and quizzes",
    "maps": "offline directions",
}


# ── THE REGISTRY — the single declarative source of truth ────────────────────
# Hand-authored, then read by every surface (introspection AND tool routing).
# `keywords` are migrated from the former hand-maintained intent buckets so the
# registry drives binding; tool names are validated against the live tool set by
# :func:`validate`.
CAPABILITIES: tuple = (
    # introspection — the self-model this module makes possible
    Capability(
        "introspection.capabilities", "introspection",
        "What PuffPAI can access and do", "describe_capabilities",
        "this capability registry", READ,
        keywords=("what can you do", "what can you access", "what can you help",
                  "your capabilities", "what are you capable", "what can i ask",
                  "what do you have access", "what all can you"),
        phrases=("what can you do", "what can you access", "what do you know",
                 "what can you help me with", "what are your capabilities"),
        note="Reads this registry — the single source of truth."),
    # weather
    Capability(
        "weather.current", "weather", "Current weather", "auto_weather_forecast",
        "OpenWeatherMap (via Home Assistant) + open-meteo AQI", READ,
        keywords=("weather", "forecast", "temperature outside", "rain", "snow",
                  "humidity", "hot", "cold today", "sunny", "windy",
                  "air quality", "aqi", "air index", "pollution", "smog",
                  "pollen", "is the air"),
        surfaces=("voice", "chat", "dashboard"),
        phrases=("what's the weather", "temperature outside", "forecast",
                 "what's the air quality outside", "how's the air outside"),
        note="Already UNIFIED at the data layer: both /dashboard/weather and "
             "auto_weather_forecast read the same HA openweathermap sensors + "
             "open-meteo. Only the code is duplicated; a "
             "shared helper would DRY it, but the values already agree."),
    Capability(
        "weather.alerts", "weather", "Severe weather alerts",
        "prep_weather_alerts", "National Weather Service / Node-RED automation",
        READ,
        keywords=("weather alert", "weather alerts", "storm", "tornado",
                  "hurricane", "severe weather", "flood", "evacuat", "warning",
                  "heat advisory", "advisory", "watches"),
        surfaces=("voice", "chat"),
        phrases=("any severe weather alerts", "is there a heat advisory",
                 "any storm warnings"),
        note="THE authority for alerts. A separate HA automation also posts "
             "alerts to notifications — reconcile both to this tool."),

    # home
    Capability(
        "home.devices", "home", "Smart-home device status", "ha_list_devices",
        "Home Assistant (~111 devices)", READ,
        also_tools=("ha_get_device_info", "ha_find_entity",
                    "ha_discover_new_devices", "ha_approve_pending_device"),
        keywords=("device", "devices", "home assistant", "what devices",
                  "list devices", "connected device", "pending device",
                  "new device", "discover"),
        agent="steward", surfaces=("voice", "chat", "dashboard"),
        phrases=("list my devices", "how many devices", "device status",
                 "temperature in the living room")),
    Capability(
        "home.control", "home", "Control lights, climate, scenes",
        "ha_control_device", "Home Assistant", READWRITE,
        also_tools=("ha_set_light", "ha_set_temperature", "ha_run_scene",
                    "ha_find_entity"),
        keywords=("light", "lamp", "bulb", "switch", "fan", "thermostat",
                  "temperature", "heat", "cool", "ac", "scene", "lock", "unlock",
                  "turn on", "turn off", "dim", "brighten", "dark", "bright",
                  "too hot", "too cold", "too warm", "freezing", "chilly",
                  "living room", "bedroom", "kitchen", "hallway", "garage",
                  "office", "manage", "add the"),
        agent="steward", surfaces=("voice", "chat"),
        phrases=("turn on the lamp", "set the thermostat", "run a scene"),
        note="State changes pass Drawbridge consent."),
    Capability(
        "home.onboarding", "home", "Pair new Zigbee/USB hardware",
        "ha_scan_hardware", "Home Assistant + ZHA", READWRITE,
        also_tools=("ha_add_zigbee_dongle", "ha_pair_zigbee_devices",
                    "ha_discover_new_devices", "ha_list_devices"),
        keywords=("zigbee", "z-wave", "zwave", "dongle", "coordinator", "pair",
                  "pairing", "permit join", "permit-join", "plug in",
                  "plugged in", "just plugged", "new hardware", "add lightbulb",
                  "add the bulb", "add bulbs", "light bulb", "lightbulb",
                  "bulbs", "scan for new", "look for new"),
        agent="steward", phrases=("pair my bulbs", "I plugged in a new dongle")),
    Capability(
        "home.provisioning", "home",
        "Propose Home Assistant scripts/automations PuffPAI needs",
        "ha_provision_propose", "Home Assistant config API (admin credential "
        "borrowed from the vault, per approved change)", WRITE,
        keywords=("home assistant script", "ha script", "create a script",
                  "automation", "helper script", "permit script",
                  "needs an administrator", "admin only", "workflow",
                  "routine", "every night", "before sunset", "after sunset",
                  "after midnight", "no motion"),
        agent="steward", surfaces=("chat", "dashboard"),
        phrases=("make a script for that", "why can't you open pairing mode"),
        note=("STATE-CHANGING but proposal-only: the tool files a consent "
              "request showing the exact configuration. Nothing is written to "
              "Home Assistant until the owner approves it with the vault "
              "unlocked, and only "
              "puffpai.ha_provisioning.apply_approved_change writes it.")),

    # cameras
    Capability(
        "cameras.alerts", "cameras", "Camera motion & visitor alerts",
        "camera_recent_alerts", "Home Assistant motion/person/visitor history",
        READ, also_tools=("camera_alerts_for", "ha_list_cameras"),
        keywords=("motion", "detected", "activity", "movement", "anyone at",
                  "who's at", "anything on the camera", "doorbell",
                  "notification", "notifications", "camera alert"),
        agent="steward",
        phrases=("any alerts from my cameras", "did the camera see anything",
                 "anyone at the door")),
    Capability(
        "cameras.tiles", "cameras", "Cameras shown on the dashboard",
        "ha_list_cameras", "Home Assistant + dashboard camera registry", READWRITE,
        also_tools=("dashboard_add_camera", "dashboard_remove_camera",
                    "ha_discover_new_devices", "sentinel_approve_device",
                    "sentinel_deny_device"),
        keywords=("camera", "cameras", "reolink", "nvr", "home hub",
                  "video feed", "live feed", "security feed"),
        agent="steward", surfaces=("voice", "chat", "dashboard"),
        phrases=("list my cameras", "add the garden camera to the dashboard"),
        note="Dashboard tiles persist in dashboard_registry.py; Drawbridge gates "
             "exposure."),

    # dashboard widgets
    Capability(
        "dashboard.widgets", "dashboard", "Dashboard widgets & feeds",
        "dashboard_list_widgets", "live dashboard widget registry", READWRITE,
        also_tools=("dashboard_add_widget", "dashboard_remove_widget"),
        keywords=("widget", "widgets", "news feed", "rss", "feed",
                  "headlines on", "add news", "tile", "dashboard",
                  "on my dashboard", "my dashboard"),
        surfaces=("voice", "chat", "dashboard"),
        phrases=("add a bbc news feed to my dashboard", "what's on my dashboard",
                 "remove the news widget"),
        note="Self-describing: widget types live in "
             "dashboard_widgets.WIDGET_CATALOG, known feeds (BBC/NPR/…) in "
             "FEED_DIRECTORY. Add/remove/list by voice via _try_dashboard_manage."),
    Capability(
        "display.show", "dashboard", "Pull content up on the screen",
        "show_on_dashboard", "offline knowledge base → dashboard viewer",
        READWRITE, also_tools=("clear_dashboard",),
        keywords=("show me the", "pull up", "bring up", "put up on",
                  "on the screen", "display the"),
        surfaces=("voice", "chat", "dashboard"),
        phrases=("show me the ted talk about surveillance",
                 "pull up the wikipedia article on solar energy",
                 "clear the screen"),
        note="Voice via _try_show → dashboard_display.show_query: resolves a "
             "knowledge search to a same-origin article URL and sets the "
             "display state the kiosk viewer renders (poll GET /dashboard/display)."),

    # calendar / tasks / timers
    Capability(
        "calendar.tasks", "calendar", "Tasks, reminders & events", "app_add_task",
        "local app data (family calendar)", READWRITE,
        also_tools=("app_add_event", "app_list_schedule", "app_complete_task",
                    "app_edit_task", "app_delete_task", "app_edit_event",
                    "app_delete_event"),
        keywords=("task", "todo", "to-do", "to do", "remind", "reminder",
                  "calendar", "event", "appointment", "meeting", "schedule",
                  "forget", "don't let me", "dont let me", "make sure i",
                  "errand", "chore", "leave in", "leaving in", "need to leave",
                  "heading out"),
        agent="majordomo", surfaces=("voice", "chat", "dashboard", "app"),
        phrases=("add a reminder", "what's on my schedule", "add an event"),
        note="One capability across surfaces: the Calendar app page, the "
             "dashboard tile, and the voice/chat answer all read this."),
    Capability(
        "calendar.timers", "calendar", "Timers & alarms", "app_set_timer",
        "local app data", READWRITE,
        also_tools=("app_set_alarm", "app_list_alarms", "app_dismiss_alarm",
                    "app_snooze_alarm", "app_delete_alarm"),
        keywords=("timer", "alarm", "wake me", "countdown", "dismiss", "snooze",
                  "silence the alarm", "cancel alarm", "delete alarm",
                  "stop the alarm", "turn off the alarm"),
        surfaces=("voice", "chat", "dashboard", "app"),
        phrases=("set a timer", "set an alarm", "dismiss the alarm"),
        note="Also the AlarmFlow app page."),

    # knowledge / documents — the always-bound fallback
    Capability(
        "knowledge.library", "knowledge", "Offline knowledge library",
        "search_knowledge_base", "FTS5 + ChromaDB offline library", READ,
        also_tools=("list_indexed_libraries",),
        keywords=("knowledge base", "library", "libraries", "look it up in"),
        agent="archivist", always=True,
        phrases=("search my knowledge base", "what libraries do you have")),
    Capability(
        "knowledge.documents", "knowledge", "Your uploaded documents",
        "search_my_documents", "uploaded docs (DocumentRegistry + ChromaDB)",
        READ, also_tools=("list_my_documents", "fetch_my_document",
                          "list_pending_suggestions"),
        keywords=("my document", "documents", "uploaded", "my file",
                  "my notes", "pending suggestion", "suggested upload"),
        agent="archivist", always=True, surfaces=("voice", "chat", "dashboard", "app"),
        phrases=("what did I upload about", "find my document"),
        note="Also the Knowledge Manager page."),

    # media (Jellyfin/Calibre)
    Capability(
        "media.library", "media", "Music, movies & books", "media_search_music",
        "Jellyfin / Calibre-Web", READWRITE,
        also_tools=("media_search_movies", "media_search_books", "media_play",
                    "media_now_playing", "media_control", "media_library_stats",
                    "media_recommend"),
        keywords=("music", "song", "album", "artist", "play", "movie", "film",
                  "watch", "book", "audiobook", "now playing", "listen", "track",
                  "jellyfin", "volume", "pause", "resume", "skip", "next track"),
        agent="steward", surfaces=("voice", "chat", "dashboard", "app"),
        phrases=("play some music", "what movies do I have", "what's playing"),
        note="Also the Media hub page."),
    Capability(
        "books.library", "media", "Book & comic library", "books_find",
        "Calibre metadata.db + Calibre-web shelves", READWRITE,
        also_tools=("books_recommend", "books_shelves", "books_shelf_add",
                    "books_shelf_remove", "books_shelf_move", "books_show"),
        keywords=("book", "books", "novel", "comic", "comics", "graphic novel",
                  "read", "reading", "shelf", "shelves", "to-read", "calibre",
                  "author", "series"),
        agent="archivist", surfaces=("voice", "chat", "dashboard", "app"),
        phrases=("what comics do we have", "recommend a mystery",
                 "put Dracula on my to-read shelf"),
        note="Shelves are Calibre-web's own, so they show up in the web "
             "reader too. Also the Media hub books page."),
    Capability(
        "media.playlists", "media", "Playlists & favorites", "media_list_playlists",
        "Jellyfin", READWRITE,
        also_tools=("media_play_playlist", "media_playlist_tracks",
                    "media_playlist_create", "media_playlist_add",
                    "media_playlist_remove", "media_favorite",
                    "media_list_favorites", "media_list_artists",
                    "media_playlist_from_artists",
                    "media_playlist_from_document"),
        keywords=("playlist", "playlists", "favorite", "favourite", "favorites",
                  "artist", "artists", "taste", "mix"),
        agent="steward", phrases=("play my playlist", "list my playlists",
                                  "make a playlist of these artists")),

    # streaming
    Capability(
        "streaming.spotify", "streaming", "Spotify", "spotify_whats_playing",
        "Spotify Connect (librespot)", READWRITE,
        also_tools=("spotify_play", "spotify_pause", "spotify_resume",
                    "spotify_next"),
        keywords=("spotify",),
        agent="steward", status=NEEDS_AUTH,
        phrases=("play on spotify", "what's playing on spotify"),
        note="One-time owner authorization in dashboard settings before "
             "playback."),
    Capability(
        "streaming.radio", "streaming", "World Radio / TuneIn", "media_radio_play",
        "radio-browser / TuneIn", READWRITE,
        also_tools=("media_radio_stations", "media_radio_favorites",
                    "media_radio_stop", "media_sources_overview"),
        keywords=("radio", "station", "stations", "tunein", "tune in", "npr",
                  "world radio", "stream ", "favorite station",
                  "favourite station", "favorite stations",
                  "favourite stations"),
        agent="steward",
        phrases=("play the radio", "what radio stations do you have")),

    # food — recipes + meal planning
    Capability(
        "food.recipes", "food", "Recipes", "app_list_recipes",
        "your recipe box + uploaded docs", READWRITE,
        also_tools=("app_create_recipe", "search_my_documents",
                    "list_my_documents"),
        keywords=("recipe", "recipes", "ingredient", "ingredients", "cook",
                  "cooking", "bake", "baking", "dish"),
        phrases=("what can I make with", "save this recipe")),
    Capability(
        "food.mealplan", "food", "Meal planning & grocery orders", "auto_meal_plan",
        "meal planner + groceries", READWRITE,
        also_tools=("meal_set", "meal_plan_show", "auto_grocery_list",
                    "grocery_order"),
        keywords=("meal", "meals", "meal plan", "dinner", "what should i make",
                  "what's for dinner", "grocery list", "groceries"),
        surfaces=("voice", "chat", "app"),
        phrases=("plan my meals", "order my groceries"),
        note="Grocery orders are confirmed purchases."),

    # health / bills
    Capability(
        "health.meds", "health", "Medications", "app_take_med", "local app data",
        READWRITE, also_tools=("app_add_med",),
        keywords=("medication", "medications", "medicine", "meds", "pill",
                  "pills", "dose", "prescription", "took my", "take my"),
        surfaces=("voice", "chat", "dashboard", "app"),
        phrases=("log my medication", "add my blood pressure medicine"),
        note="Also the fitness page."),
    Capability(
        "bills.manage", "bills", "Bills", "app_add_bill", "local app data",
        READWRITE, also_tools=("app_pay_bill",),
        keywords=("bill", "bills", "pay ", "payment", "invoice", "rent",
                  "utilities", "electric bill", "past due", "due"),
        surfaces=("voice", "chat", "dashboard", "app"),
        phrases=("add my electric bill", "mark the bill paid")),

    # prep / fitness / pantry
    Capability(
        "prep.supplies", "prep", "Emergency supplies", "prep_days_remaining",
        "emergency-prep inventory", READWRITE,
        also_tools=("prep_view_inventory", "prep_check_expiration",
                    "prep_recommendations", "prep_add_supply", "save_prep_item",
                    "prep_set_family_size", "prep_set_location"),
        keywords=("supplies", "supply", "expire", "expiring", "expiration",
                  "emergency", "preparedness", "prep ", "survival", "stockpile",
                  "shelf life", "shelf-life", "canned", "days of food",
                  "days of water", "how much food", "how much water",
                  "enough food", "enough water", "food do we have",
                  "water do we have", "rations", "family size",
                  "set my location", "set location"),
        phrases=("how many days of food", "what supplies are expiring")),
    Capability(
        "fitness.training", "fitness", "Workouts & records", "app_fitness_status",
        "local app data", READWRITE,
        also_tools=("app_log_workout", "app_log_set", "app_delete_workout",
                    "app_personal_records", "app_generate_workout",
                    "app_list_goals"),
        keywords=("workout", "lift", "lifted", "lifting", "bench", "squat",
                  "deadlift", "press", "row", "curl", "rep ", "reps", "set ",
                  "sets", "1rm", "pr ", "personal record", "personal records",
                  "fitness", "fitfam", "gym", "exercise", "muscle", "warmup",
                  "warm up", "rpe", "goals"),
        surfaces=("voice", "chat", "dashboard", "app"),
        phrases=("log my workout", "what are my personal records",
                 "generate a workout"),
        note="Also the Fitness page."),
    Capability(
        "pantry.shopping", "pantry", "Pantry & shopping list", "app_list_shopping",
        "local app data", READWRITE,
        also_tools=("app_add_shopping", "app_list_pantry", "app_add_pantry",
                    "app_check_shopping", "app_remove_pantry"),
        keywords=("shopping", "shop", "grocery", "groceries", "pantry", "fridge",
                  "eat", "ate", "bought", "buy", "food", "kitchen",
                  "remove from pantry", "used up", "ran out of", "check off",
                  "cross off"),
        surfaces=("voice", "chat", "dashboard", "app"),
        phrases=("add milk to the shopping list", "what's in the pantry"),
        note="Also the FreshTable app page."),

    # security / network / vault
    Capability(
        "security.network", "security", "Network scan & privacy",
        "privacy_scan_network", "DeviceSentinel / router scan", READWRITE,
        also_tools=("privacy_block_device", "privacy_unblock_device",
                    "privacy_lockdown_mode", "ha_block_device_internet",
                    "ha_scan_hardware"),
        keywords=("block", "unblock", "firewall", "lockdown", "scan", "network",
                  "device security", "unknown device", "privacy",
                  "block internet", "cut off internet", "no internet for"),
        agent="sentinel", surfaces=("voice", "chat", "dashboard"),
        phrases=("scan my network", "block a device", "turn on lockdown"),
        # Each of these names THIS house's network and nothing else. The bare
        # word "network" is deliberately absent: "what is a social network"
        # and "network theory" are ordinary English, and dispatching on them
        # answered a maths question with a router scan.
        dispatch_phrases=("network status", "the network status",
                          "my network status", "what is the network status",
                          "what's the network status", "whats the network status",
                          "what is my network status", "what's my network status",
                          "status of my network", "check my network status",
                          "scan my network", "network scan", "run a network scan",
                          "who is on my network", "who's on my network",
                          "whos on my network")),
    Capability(
        "security.zone3", "security", "Zone-3 network edge", "zone3_status",
        "Zone-3 network edge (AdGuard sinkhole via puffpai.zone3.engine)",
        READWRITE,
        also_tools=("zone3_discover_devices", "zone3_secure_device",
                    "zone3_change_mode", "zone3_enforce"),
        keywords=("zone 3", "zone three", "network edge", "perimeter",
                  "edge security", "enforcement", "enforcing", "telemetry",
                  "phone home", "phoning home", "sinkhole", "adguard",
                  "dns filter", "dns filtering",
                  # The words the tool ADVERTISES. ``zone3_enforce``'s
                  # docstring has said "stop filtering my network" and "start
                  # blocking telemetry" since it shipped, and neither bound a
                  # zone3 tool: "stop filtering my network" matched nothing
                  # here and went to security.network, and "start blocking
                  # telemetry" scored a four-way tie that ``find`` broke by
                  # declaration order, landing on home.control — so the
                  # advertised sentence reached the light switch. A tool that
                  # names a phrase it cannot be reached by is worse than one
                  # that names none: it reads as coverage.
                  "filtering", "filter my network", "unfiltered",
                  "start blocking", "stop blocking", "block the telemetry"),
        agent="sentinel", surfaces=("voice", "chat", "dashboard"),
        # Binding signal, NOT dispatch. These stay out of ``dispatch_phrases``
        # on purpose: dispatch answers with the capability's AUTHORITY tool,
        # which here is the read-only ``zone3_status``, so auto-dispatching
        # "turn on enforcement" would read the status out loud and call it
        # done. Binding hands the model ``zone3_enforce``, which returns a
        # proposal and actuates nothing — safe to reach, and the only way the
        # owner ever sees the confirmation step.
        phrases=("what's my zone 3 status", "turn on enforcement",
                 "turn off enforcement", "turn enforcement on",
                 "turn enforcement off", "block telemetry",
                 "block my tv's telemetry", "stop my tv from phoning home",
                 "start blocking telemetry", "stop blocking telemetry",
                 "stop filtering my network", "start filtering my network",
                 "stop filtering the network"),
        # WRITE tools here never actuate from a tool call. zone3_enforce,
        # zone3_secure_device and zone3_change_mode return a PROPOSAL and stop;
        # the owner confirms on the Firewall page, which redeems a single-use
        # consent ticket server-side. So the model can describe the change and
        # cannot make it — which is the point, because a tool the model can call
        # is exactly the surface a prompt injection reaches.
        note="State-changing tools propose only; the owner's confirmation on "
             "the Firewall page is what actuates."),
    Capability(
        "bridge.inventory", "security", "The Bridge — network device roll",
        "bridge_list_devices",
        "bridge_inventory (ARP + mDNS + Home Assistant + AdGuard) and "
        "bridge_ledger (AdGuard query log)", READ,
        also_tools=("bridge_device_telemetry", "bridge_status",
                    "bridge_refresh", "bridge_ha_buckets",
                    "privacy_scan_network"),
        keywords=("what devices", "devices on my network", "on my network",
                  "my devices", "device list", "new device", "new devices",
                  "talking to", "reaching out", "who is my", "what is my tv",
                  "phoning home", "telemetry", "the bridge", "device roll",
                  "device inventory"),
        agent="sentinel", surfaces=("voice", "chat", "dashboard"),
        phrases=("what devices are on my network", "what's on my network",
                 # Declared in full, and not only as a dispatch phrase, because
                 # ``find`` never reads dispatch phrases. Scored on keywords
                 # alone this sentence tied with ``security.network`` (both hit
                 # "network" and both have it in their label) and lost on
                 # declaration order, so "what is on my network" was answered
                 # by the router scanner rather than by the device roll. An
                 # exact phrase scores 10 and settles it.
                 "what is on my network",
                 "whats on my network", "list my devices",
                 "what is my tv talking to", "what's my tv talking to",
                 "how many smart devices talk to the cloud",
                 "what does home assistant talk to",
                 "new devices", "any new devices on my network"),
        # Read-only, and every phrase names THIS house's network, so these are
        # safe to dispatch: there is no other thing "what devices are on my
        # network" could mean. The bare word "devices" is deliberately absent —
        # "list devices" is the Steward's HA roll, a different question.
        dispatch_phrases=("what devices are on my network",
                          "what devices are on the network",
                          "what's on my network", "whats on my network",
                          "what is on my network",
                          "list the devices on my network",
                          "what new devices are on my network",
                          "any new devices on my network",
                          "how many smart devices talk to the cloud",
                          "what does home assistant talk to"),
        note="Read-only. The observe half of Zone 3: it sees and records, and "
             "changes nothing."),
    Capability(
        "security.consent", "security", "Device consent & approvals",
        "sentinel_list_requests", "Drawbridge consent store", READWRITE,
        also_tools=("sentinel_approve_device", "sentinel_deny_device",
                    "ha_approve_pending_device"),
        keywords=("approve", "approval", "needs approval", "consent",
                  "permission", "grant", "authorize", "go ahead", "allow it",
                  "don't allow", "deny it"),
        agent="sentinel", phrases=("approve the new device", "any pending devices")),
    Capability(
        "vault.secrets", "vault", "Private vault (spoken-PIN)", "vault_list",
        "encrypted vault (HMAC PIN, fail-closed)", READ,
        also_tools=("vault_unlock_and_get",),
        keywords=("vault", "my password", "my passwords", "secret", "secrets",
                  "credential", "credentials", "my pin", "locked", "private key",
                  "api key", "stored password", "get my"),
        agent="archivist", surfaces=("voice", "chat", "dashboard", "app"),
        phrases=("what's in my vault", "unlock my home assistant token"),
        # Every phrase must name the VAULT itself. Held tighter than the other
        # capabilities because vault_list needs no PIN and answers "you have N
        # protected items: your router password, your bank PIN...", so a loose
        # trigger volunteers the inventory of what secrets exist, unprompted,
        # and writes an audit entry each time. The keywords "secret", "locked",
        # "get my", "my pin", "private key" are therefore deliberately NOT
        # dispatch phrases -- "tell me a secret" and "is the front door locked"
        # are ordinary English, and both read out the vault in a first attempt.
        # The lock-state phrasings USED to live here, dispatching to
        # vault_list. That was non-responsive by construction — vault_list
        # lists labels, so "is the vault locked" was answered with "your vault
        # is set up but empty", and on a populated vault it would have
        # volunteered the inventory of what secrets exist. They now belong to
        # vault.status below, which reports locked/unlocked and a count only.
        dispatch_phrases=("what is in my vault", "what's in my vault",
                          "whats in my vault",
                          "what is in the vault", "what's in the vault",
                          "list my vault", "my vault contents"),
        note="Spoken PIN; 5 wrong tries = 5-minute lockout; value never logged."),
    Capability(
        "vault.status", "vault", "Whether the vault is locked", "vault_status",
        "encrypted vault (lock state + item count only)", READ,
        keywords=("vault locked", "vault unlocked", "vault status",
                  "vault open", "vault sealed"),
        agent="archivist", surfaces=("voice", "chat", "dashboard"),
        phrases=("is the vault locked", "vault status"),
        # Safe to dispatch widely where vault.secrets is not: this answers
        # LOCKED/UNLOCKED and a bare count. It names no item, so a match on a
        # question that was not really about the vault leaks nothing.
        dispatch_phrases=("vault status", "the vault status", "my vault status",
                          "what is my vault status", "what's my vault status",
                          "whats my vault status", "status of my vault",
                          "what is the vault status", "what's the vault status",
                          "whats the vault status",
                          "is the vault locked", "is my vault locked",
                          "is our vault locked",
                          "is the vault unlocked", "is my vault unlocked",
                          "is the vault open", "is my vault open",
                          "is the vault still locked",
                          "is my vault still locked"),
        note="Answers the lock-state question vault_list cannot: it lists "
             "labels, and the labels are the sensitive part."),
    Capability(
        "vault.display", "vault", "Show a protected document from the vault",
        "vault_display_document",
        "Zone-1 encrypted document index (PIN-gated, ephemeral serve)", READ,
        keywords=("birth certificate", "death certificate",
                  "marriage certificate", "passport", "social security", "ssn",
                  "driver's license", "drivers license", "state id",
                  "medical record", "health record", "prescription",
                  "immunization record", "insurance card", "tax return",
                  "bank statement", "bank account", "will", "deed", "mortgage"),
        agent="archivist", status=NEEDS_AUTH,
        surfaces=("voice", "chat", "dashboard"),
        phrases=("show me a birth certificate", "pull up my passport",
                 "show my Social Security card"),
        note=("Zone-2 boundary: the brain recognizes the doc TYPE only and holds "
              "NO index of vault contents. The PIN is captured by a secure "
              "fastpath and is NEVER an LLM tool argument. The decrypted doc is "
              "served ephemerally, loopback-only, and auto-clears.")),

    # briefing / news
    Capability(
        "briefing.daily", "briefing", "Daily briefing", "auto_daily_briefing",
        "composed from calendar/weather/news", READ,
        keywords=("briefing", "morning update", "brief me", "daily update",
                  "daily report", "catch me up"),
        surfaces=("voice", "chat"), phrases=("give me my daily briefing",)),
    Capability(
        "news.headlines", "news", "News headlines", "auto_news_summary",
        "Node-RED daily_news feed", READ,
        keywords=("news", "headlines", "current events"),
        surfaces=("voice", "chat", "dashboard"),
        phrases=("what's in the news", "today's headlines"),
        note="A web-search news variant needs the egress opt-in "
             "(agents.chat_allow_egress, off by default)."),

    # agents
    Capability(
        "agents.roster", "agents", "The agent team", "list_agents",
        "agent bus / roster", READ, agent="concierge",
        keywords=("agent", "agents", "your team", "the team", "who can",
                  "which agent", "what agent", "sub-agent", "subagent",
                  "the roster", "dispatcher", "majordomo", "steward", "sentinel",
                  "archivist", "caretaker", "toolsmith", "concierge"),
        phrases=("what agents are on your team", "who's on your team")),
    Capability(
        "agents.task", "agents", "Task one agent", "ask_agent", "agent bus",
        READWRITE, agent="concierge",
        keywords=("have the ", "ask the ", "tell the ", "route this",
                  "route to", "hand this to"),
        phrases=("ask the steward how many devices", "ask the caretaker if healthy")),

    # system health (caretaker)
    Capability(
        "system.health", "system", "System health check", "auto_health_check",
        "Caretaker probes", READ, agent="caretaker",
        keywords=("health check", "system health", "are you healthy",
                  "self test", "self-test", "diagnostic", "diagnostics",
                  "everything ok", "everything okay", "run a checkup"),
        surfaces=("voice", "chat", "dashboard"),
        phrases=("run a system health check", "are all your services up")),

    # utility — calculator, sandbox files, web search
    Capability(
        "utility.calculate", "utility", "Calculator", "calculate",
        "on-device math", READ,
        keywords=("calculate", "compute", " math", " times ", " plus ",
                  " minus ", "divided by", "multiply", "multiplied", "divide",
                  "sum of", "add up", "subtract", "percentage", "percent of",
                  "square root", "power of", "squared", "cubed", "how much is"),
        phrases=("what's 12 times 8", "calculate the tip")),
    Capability(
        "utility.files", "utility", "Sandbox files", "read_file",
        "sandboxed workspace", READWRITE, also_tools=("write_file",),
        keywords=("create file", "write file", "save to", "write to",
                  "read file", "open file", "show file", "the file"),
        phrases=("write that to a file", "read the file")),
    Capability(
        "utility.registry_read", "utility", "Reach a read-only capability by name",
        "registry_read", "the capability registry", READ,
        keywords=(), phrases=(),
        note="Bridge from a planned step to the read-only tool tier. READ "
             "capabilities owned by toolsmith/archivist/majordomo only; the "
             "vault is denied by id and it cannot invoke itself. No keywords: "
             "it is a composition primitive, never something a person asks "
             "for by name."),
    Capability(
        "utility.documents", "utility", "Read and convert documents",
        "document_convert", "LibreOffice in the sandbox", READWRITE,
        keywords=("word doc", "word document", ".docx", "spreadsheet",
                  ".xlsx", "powerpoint", "convert to pdf", "as a pdf",
                  "read the document", "open the document", "export to"),
        phrases=("read my word doc", "convert that to a pdf",
                 "turn this into a word document"),
        note="Uses the LibreOffice already on the appliance, inside the same "
             "bubblewrap jail as utility.exec (no network, workspace only). "
             "HTML input is not supported by this build; HTML output is."),
    Capability(
        "skills.manage", "skills", "Skills you approved", "(none)",
        "the skills store (sandboxed packages)", READWRITE, agent="toolsmith",
        surfaces=("voice", "chat", "dashboard"),
        keywords=("skill", "skills"),
        phrases=("what skills do i have", "list my skills",
                 "remove a skill", "disable a skill"),
        dispatch_phrases=("what skills do i have", "what skills do you have",
                          "list my skills", "list your skills",
                          "what skills are installed"),
        note="Lists, disables and removes the sandboxed skills you approved. "
             "Installing one always needs a consent card with the code on it; "
             "no skill can be added by voice alone."),
    Capability(
        "utility.exec", "utility", "Run a command in the sandbox",
        "workspace_exec", "sandboxed workspace (no network)", READWRITE,
        keywords=("run a command", "run the command", "shell", "script",
                  "convert the file", "rename the files", "count the lines"),
        phrases=("run a command in the sandbox", "convert that file"),
        note="Runs inside a bubblewrap jail with no network and no path "
             "outside the workspace; every run is state-changing, so it "
             "goes through Drawbridge review first."),
    Capability(
        "utility.websearch", "utility", "Web search", "web_search",
        "web (egress-gated)", READ, status=LIVE, agent="toolsmith",
        keywords=("search the web", "look up", "google", "search for",
                  "latest", "search online", "research", "find out",
                  "online", "on the web"),
        phrases=("search the web for", "look it up online",
                 "find out online", "research that on the web"),
        note="Runs through the household's OWN SearXNG search box on this "
             "appliance, which queries only the engine configured in SearXNG "
             "(currently read live) and nothing else; every lookup is "
             "written to the egress ledger. Gated by "
             "the chat egress opt-in AND the web_search door — both off by "
             "default. Status here is LIVE and PROBED (capability_probes), "
             "not declared: a hard-coded needs-auth status here once made "
             "PuffPAI tell the owner web search needed setting up while it "
             "was answering live searches in under a second."),

    # dev — the on-demand coding model (config.ollama.code_model)
    Capability(
        "dev.code", "dev", "Write code",
        "write_code",
        "local coding model (on-demand, never pinned) — AUTHORS code, "
        "scripts, functions and automations (generation only)", READ,
        agent="toolsmith",
        keywords=("code", "script", "function", "program", "snippet",
                  "automation", "flow", "node-red", "nodered", "systemd",
                  "unit file", "python", "javascript", "bash", "regex",
                  "regular expression", "fix this code", "refactor",
                  "write code", "write a script", "write me a script",
                  "rename files", "batch", "help me write", "write me",
                  "code for", "automation script", "node-red flow"),
        phrases=("write me a python script", "write a python script",
                 "write a script that", "write me a function",
                 "write a function that", "generate a bash script",
                 "create a node-red flow", "write me some code",
                 "fix this code", "refactor this function",
                 "write a regex", "write me a program",
                 "i need a small script to rename my photo files",
                 "help me write a script", "can you help me write a script",
                 "write me a script to rename files"),
        note="Generation ONLY — it writes code and shows it to you; it never "
             "runs it, saves it, or changes this appliance. Running something "
             "it wrote is a separate, sandboxed step (utility.exec). The "
             "coding model is loaded on demand and released again minutes "
             "later, so it costs no memory until you ask."),

    # learning — trivia
    Capability(
        "learning.trivia", "learning", "Trivia & quizzes", "app_trivia_question",
        "local trivia generator", READWRITE,
        also_tools=("app_trivia_generate", "app_trivia_stats"),
        keywords=("trivia", "quiz", "question game", "test me", "quiz me",
                  "study", "curriculum", "ask me a question",
                  # the round, not just the question: grading, score, history,
                  # difficulty, topic and stop are all one capability now
                  "my score", "quiz history", "trivia history", "quiz score",
                  "harder questions", "easier questions", "stop the quiz",
                  "play trivia", "next question"),
        phrases=("quiz me on", "ask me a trivia question",
                 "lets play a quiz", "play trivia about space",
                 "quiz me on the solar system", "give me harder questions",
                 "give me easier questions", "what's my score",
                 "what is our quiz history", "stop the quiz",
                 "play trivia for the kids"),
        note="One persisted round per household: questions are served without "
             "the answer key, answers are graded with feedback, and the score "
             "and history are read back from the trivia app data rather than "
             "from chat context. A topic outside the bundled bank goes to the "
             "local generator; questions are filtered by the player's age "
             "band, and the age itself is never spoken."),

    # household — WHO the appliance is for. No tool: the roster is a STORE
    # (profiles.json), read and written by puffpai.household, and a capability
    # whose authority is a store rather than a tool says so rather than
    # inventing a tool to point at.
    Capability(
        "home.household", "home", "Who's in the household", "(none)",
        "profiles.json (puffpai.household) + the apps it syncs", READWRITE,
        agent="majordomo", surfaces=("voice", "chat", "dashboard"),
        keywords=("who lives here", "household roster", "household members",
                  "family members", "who is in my household",
                  "who is in my family"),
        phrases=("who is in my family", "add Riley to my household"),
        note="One roster; the Calendar family list, FitFam users and Trivia "
             "players are projections of it."),

    # maps: offline directions from the box's own engines. No
    # tool: the authority is the _try_maps_directions fastpath over two
    # loopback engines. Status is PROBED (capability_probes): offline when
    # either engine is not answering.
    Capability(
        "maps.directions", "maps", "Offline directions", "(none)",
        "the box's own map engines (Valhalla routing + Photon place search, "
        "OpenStreetMap data)", READ, status=LIVE,
        surfaces=("voice", "chat", "dashboard"),
        keywords=("directions", "navigate", "how do i get to", "route to",
                  "map to"),
        phrases=("give me directions to", "how do I get to",
                 "where's the nearest library"),
        note="Answers from the box only, starting at the home point set in "
             "Settings; the steps go to the screen and the chat, with an "
             "Organic Maps link that has no start point."),
)


# ── agent ownership fill-in ──────────────────────────────────────────────────
# Every capability names the roster agent that answers for it, so the Agent
# Team page can show each agent the FULL set of what it actually does — not just
# the handful that happened to declare an owner inline. Capabilities left without an ``agent=`` above are assigned here by
# role rather than by editing each literal. This is display/introspection only:
# the router binds tools on keywords, not on this field (see
# :func:`tools_for_query`), and :func:`validate` still checks every id against
# the live roster, so a bad id here trips a test.
_AGENT_FILL = {
    "introspection.capabilities": "concierge",
    "weather.current": "majordomo",
    "weather.alerts": "majordomo",
    "dashboard.widgets": "steward",
    "display.show": "steward",
    "calendar.timers": "majordomo",
    "food.recipes": "archivist",
    "food.mealplan": "majordomo",
    "health.meds": "caretaker",
    "bills.manage": "majordomo",
    "prep.supplies": "caretaker",
    "fitness.training": "caretaker",
    "pantry.shopping": "steward",
    "briefing.daily": "majordomo",
    "news.headlines": "majordomo",
    "utility.calculate": "toolsmith",
    "utility.files": "toolsmith",
    "utility.exec": "toolsmith",
    "utility.documents": "toolsmith",
    "utility.registry_read": "toolsmith",
    "learning.trivia": "archivist",
    "maps.directions": "toolsmith",
}
CAPABILITIES = tuple(
    replace(c, agent=_AGENT_FILL[c.id])
    if (c.agent is None and c.id in _AGENT_FILL) else c
    for c in CAPABILITIES
)


# ── indexes (built once at import) ───────────────────────────────────────────
_BY_ID = {c.id: c for c in CAPABILITIES}

#: A digit followed by a math operator followed by a digit → the calculator.
#: (The former intent bucket matched this regex in addition to keywords.)
_MATH_RE = re.compile(r"\d\s*[-+*/^×÷]\s*\d")


# ── the skill overlay ────────────────────────────────────────────────────────
#
# The static tuple above stays exactly as it is: it is the appliance's declared
# contract and 40-odd tests read it directly. Installed SKILLS are an overlay on
# top of it — rows contributed at run time by
# :func:`puffpai.skills.store.capabilities`, replaced wholesale on every rescan
# so a removed skill's row cannot outlive it.
#
# ``_SKILL_BY_ID`` exists for one reason: :func:`get` is how every other module
# turns a capability id into a row (``chat_activity`` attributes a turn with it,
# ``runtime_status`` probes with it). A ``get`` that could not see a skill would
# make an installed skill invisible to attribution and to status while still
# being dispatchable — which is the dishonest half of both.
_SKILL_ROWS: tuple = ()
_SKILL_BY_ID: dict = {}
_SKILL_OVERLAY_LOADED = False


def set_skill_overlay(rows) -> tuple:
    """Replace the skill rows. Called by ``skills.store.rescan()`` only."""
    global _SKILL_ROWS, _SKILL_BY_ID, _SKILL_OVERLAY_LOADED
    clean = tuple(r for r in (rows or ()) if isinstance(r, Capability)
                  and r.id not in _BY_ID)
    _SKILL_ROWS = clean
    _SKILL_BY_ID = {r.id: r for r in clean}
    _SKILL_OVERLAY_LOADED = True
    return clean


def clear_skill_overlay() -> None:
    """Forget the skill rows (a build with no skills, and tests)."""
    set_skill_overlay(())


def _ensure_skill_overlay() -> None:
    """Load the overlay once, lazily. Never raises, never recurses.

    The flag is set BEFORE the import so that a skills module which asks the
    registry a question during its own load gets the static tuple rather than a
    second trip through here.
    """
    global _SKILL_OVERLAY_LOADED
    if _SKILL_OVERLAY_LOADED:
        return
    _SKILL_OVERLAY_LOADED = True
    try:
        from puffpai.skills import store as _store
        set_skill_overlay(_store.capabilities())
    except Exception:  # a skills fault must never break the registry
        set_skill_overlay(())


def skill_capabilities() -> tuple:
    """Just the skill rows, in install order."""
    _ensure_skill_overlay()
    return _SKILL_ROWS


def all_capabilities() -> tuple:
    """The declared capabilities, then any installed skills."""
    _ensure_skill_overlay()
    return CAPABILITIES + _SKILL_ROWS


def get(cap_id: str) -> Capability | None:
    found = _BY_ID.get(cap_id)
    if found is not None:
        return found
    _ensure_skill_overlay()
    return _SKILL_BY_ID.get(cap_id)


def domains() -> list:
    """Domains present, in human order, each with its capability count."""
    seen: dict = {}
    for c in all_capabilities():
        seen.setdefault(c.domain, 0)
        seen[c.domain] += 1
    ordered = [d for d in DOMAIN_ORDER if d in seen]
    ordered += [d for d in seen if d not in DOMAIN_ORDER]
    return [{"domain": d, "count": seen[d]} for d in ordered]


def by_domain(domain: str) -> list:
    d = (domain or "").strip().lower()
    return [c for c in all_capabilities() if c.domain == d]


def authority_for(cap_id: str) -> str | None:
    c = get(cap_id)
    return c.authority_tool if c else None


def runtime_status(c: Capability) -> str:
    """A capability's status as it is RIGHT NOW, not as it was declared.

    For most capabilities that is the declared status. For the few that depend
    on something which can be off (Zone 3's network edge), it is whatever
    ``capability_probes`` measures — otherwise the registry keeps advertising a
    feature the box stopped having. Never raises: an unprobeable capability
    keeps its declaration."""
    try:
        from puffpai import capability_probes
    except Exception:  # pragma: no cover - always ships together
        return c.status
    try:
        return capability_probes.status_for(c.id, c.status)
    except Exception:  # pragma: no cover - probe() is already total
        return c.status


def runtime_availability(c: Capability) -> tuple:
    """``(available, reason)`` for a capability right now.

    Distinct from :func:`runtime_status` on purpose. A capability can be LIVE
    and still have part of it unavailable — ``security.network``'s scan works
    on the appliance while its block/lockdown half cannot run there at all
    (see ``firewall_manager``). Status answers "is this area up"; this answers "will the
    action actually happen if I ask for it", which is what a surface must not
    get wrong about a security control."""
    try:
        from puffpai import capability_probes
        return capability_probes.probe(c.id)
    except Exception:  # pragma: no cover - probe() is already total
        return True, ""


def live_surfaces(cap) -> tuple:
    """A capability's surfaces, with "app" dropped when its app is not installed.

    A capability names the surfaces it is reachable through, and one of them —
    "app" — is a claim about a page that some INSTALLED app provides. Under the
    app contract that page can be removed by deleting a folder, so the claim is
    resolved against the registry rather than trusted: uninstall FreshTable and
    the pantry capability stops offering "open the app", while keeping voice and
    chat, which still work.

    Never raises and never ADDS a surface: a registry that cannot be read leaves
    the declared tuple exactly as written, which is the behaviour this build had
    before manifests existed.
    """
    surfaces = tuple(getattr(cap, "surfaces", ()) or ())
    if "app" not in surfaces:
        return surfaces
    try:
        from puffpai import app_registry
        app_id = app_registry.CAPABILITY_APPS.get(getattr(cap, "id", ""))
        if app_id is None:
            return surfaces
        installed = app_registry.installed_ids()
        if not installed or app_id in installed:
            return surfaces
    except Exception:
        return surfaces
    return tuple(s for s in surfaces if s != "app")


def _serialize(c: Capability) -> dict:
    """One capability as a JSON-safe dict for a surface (the dashboard) to
    render. Deliberately small: the fields a card needs to say *what* the
    capability is and *how healthy/reachable* it is, not the routing internals."""
    available, reason = runtime_availability(c)
    return {
        "id": c.id,
        "domain": c.domain,
        "label": c.label,
        "access": c.access,          # read / write / read-write
        "status": runtime_status(c),  # live / needs-auth / offline / planned
        # Probed, not declared. A card must be able to grey out
        # an action PuffPAI cannot perform instead of offering a button that
        # will refuse.
        "available": available,
        "unavailable_reason": reason,
        "surfaces": list(live_surfaces(c)),
        "note": c.note,
        # Empty for every declared capability; "skill:<id>" for a row an
        # installed skill contributed. A surface that cannot tell the two apart
        # cannot let a household remove one of them.
        "origin": getattr(c, "origin", ""),
    }


def by_agent(agent_id: str) -> list:
    """The capabilities one roster agent OWNS, in human domain order.

    Only capabilities that declare ``agent=<id>`` are returned — the registry
    is the single source of truth for which agent answers for what, so the
    dashboard shows the same ownership the router uses instead of a second,
    drifting list."""
    a = (agent_id or "").strip().lower()
    order = {d: i for i, d in enumerate(DOMAIN_ORDER)}
    caps = [c for c in all_capabilities() if (c.agent or "").lower() == a]
    caps.sort(key=lambda c: (order.get(c.domain, 999), c.label))
    return caps


def serialized_by_agent() -> dict:
    """``agent_id -> [serialized capability]`` for every agent that owns at
    least one capability. Built from the same ``agent=`` declarations the
    router reads, so "what the Agent Team page shows Steward doing" and "what
    the brain actually routes to Steward" cannot disagree."""
    order = {d: i for i, d in enumerate(DOMAIN_ORDER)}
    grouped: dict = {}
    for c in all_capabilities():
        if c.agent:
            grouped.setdefault(c.agent, []).append(c)
    return {
        agent: [
            _serialize(c)
            for c in sorted(caps, key=lambda c: (order.get(c.domain, 999), c.label))
        ]
        for agent, caps in grouped.items()
    }


def _cap_matches(c: Capability, low: str) -> bool:
    for k in c.keywords:
        if k in low:
            return True
    for p in c.phrases:
        if p in low:
            return True
    return False


def tools_for_query(query: str, live_names=None) -> list:
    """Registry-driven tool binding: the tool names to bind for a query.

    This REPLACES the former ~250-line hand-maintained keyword wall in
    ``AssistantTools._filter_tools_by_intent``. Each capability declares its own
    trigger keywords, so the tool that OWNS the capability gets bound when the
    user's words hit it — routing now comes from the single source of truth.

    Always binds the knowledge fallback (capabilities marked ``always``). If
    ``live_names`` is given, the result is intersected with it (so a
    not-yet-shipped tool never gets offered). Note: the stateful consent bind
    (open Sentinel requests) stays in the caller — it depends on runtime state,
    not the words.
    """
    low = (query or "").lower()
    names: set = set()
    for c in all_capabilities():
        if c.always or _cap_matches(c, low):
            names.update(c.all_tools)
    if _MATH_RE.search(low):
        names.add("calculate")
    if live_names is not None:
        names &= set(live_names)
    return sorted(names)


def _domain_of_query(query: str) -> str | None:
    """Best-effort: which domain a free-text question is about (for
    'what can you do with my <X>'). Matches domain names and synonyms."""
    low = (query or "").lower()
    for d in DOMAIN_ORDER:
        if d in low:
            return d
    synonyms = {
        "camera": "cameras", "device": "home", "light": "home",
        "thermostat": "home", "reminder": "calendar", "task": "calendar",
        "timer": "calendar", "alarm": "calendar", "document": "knowledge",
        "library": "knowledge", "music": "media", "movie": "media",
        "spotify": "streaming", "recipe": "food",
        "meal": "food", "medication": "health", "med": "health",
        "bill": "bills", "supply": "prep", "workout": "fitness",
        "pantry": "pantry", "shopping": "pantry", "network": "security",
        "secret": "vault", "password": "vault", "briefing": "briefing",
        "news": "news", "agent": "agents", "trivia": "learning",
        "calculat": "utility", "direction": "maps",
    }
    for word, dom in synonyms.items():
        if word in low:
            return dom
    return None


def find(query: str) -> Capability | None:
    """Best-matching capability for a free-text question — the seed of a
    registry-driven router. Scores on exact phrase/keyword, then label."""
    low = (query or "").lower().strip()
    if not low:
        return None
    best, best_score = None, 0
    for c in all_capabilities():
        score = 0
        for p in c.phrases:
            if p in low:
                score = max(score, 10)
            else:
                toks = [t for t in p.split() if len(t) > 3]
                hit = sum(1 for t in toks if t in low)
                if toks:
                    score = max(score, hit * 2)
        for k in c.keywords:
            if k.strip() and k in low:
                score = max(score, 4)
        for t in c.label.lower().split():
            if len(t) > 3 and t in low:
                score += 1
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= 3 else None


#: Politeness and address that wrap a request without changing it. Stripped
#: before matching so "hey puff, network status please" is still the same
#: utterance. Kept SHORT on purpose — every entry here is surface area, and a
#: phrase that needs a long prefix list to match probably is not unambiguous.
#:
#: A trailing half one word wide (please/thanks) is not enough: "what is the
#: network status" answers from the router in a fraction of a second, while
#: "tell me the network status too" would normalise to "the network status
#: TOO", miss the equality match, fall to RAG and come back explaining an
#: automotive LIN bus and HTTP status codes as though they were this
#: household's network. The prefix half carries "tell me"; the conversational
#: TAIL needs its own list. Both halves are bounded lists,
#: and every entry is a pure conversational wrapper: none of them can change
#: WHICH read is being asked for.
#:
#: Checked before widening: no declared ``dispatch_phrase`` in the registry
#: contains any of these words, so no phrase can be trimmed into a different
#: phrase. See ``tests/test_clause_normalize_wrappers.py``.
_UTTERANCE_TRIM_RE = re.compile(
    r"^(?:hey\s+puff(?:pai)?|puff(?:pai)?|ok(?:ay)?|hi|hello)\b[\s,]*"
    r"|^(?:please|can\s+you|could\s+you|would\s+you|tell\s+me"
    r"|also|and|too|just|go\s+ahead\s+and)\b[\s,]*"
    # ``\b`` before the alternation, symmetric with the two leading patterns.
    # Without it the trailing trim bites into a word: "network statusthanks"
    # trimmed to "network status" and dispatched. No English sentence made that
    # a WRONG answer, but a trim that can eat part of a word is not a property
    # to leave standing under a matcher whose whole correctness argument is
    # exact equality.
    r"|[\s,]*\b(?:please|thanks|thank\s+you|too|also|as\s+well"
    r"|for\s+me|right\s+now|now)\s*$",
    re.IGNORECASE)


def _normalize_utterance(text: str) -> str:
    """Lowercase, unpunctuated, politeness-stripped form of what was said."""
    low = (text or "").strip().lower()
    low = re.sub(r"[‘’“”]", "'", low)
    low = re.sub(r"\s+", " ", low)
    prev = None
    while prev != low:                       # peel "hey puff, please ..."
        prev = low
        low = _UTTERANCE_TRIM_RE.sub("", low).strip()
    return low.strip(" .!?,;:\"'")


def find_for_dispatch(query: str) -> "Capability | None":
    """The capability whose read tool may be invoked for *query*, or None.

    Deliberately unlike :func:`find`, which is a RANKING function: find always
    names a best guess, scoring on keywords and on partial token overlap, and
    that is right for "which capability is this probably about" and wrong for
    "invoke this tool now". This consults ``dispatch_phrases`` ONLY — never
    ``keywords``, never ``phrases``, never a score.

    Keywords are excluded on purpose and not as an oversight. They are a
    binding signal: ``calendar.timers`` declares ``dismiss``, ``snooze`` and
    ``silence the alarm`` among its keywords, because that list exists to bind
    the alarm tools rather than to answer with one. Dispatching on it returned
    the alarm LIST to "silence my alarm" while the alarm kept ringing.

    The match is on the WHOLE UTTERANCE, not containment. That distinction is
    the entire correctness argument, and it was learned twice:

    * driving dispatch off ``keywords`` read out the vault for "tell me a
      secret";
    * driving it off declared phrases but still matching by CONTAINMENT read
      out the vault for "is the vault locked in the heist movie" and ran a
      router scan for "what is my sister's network status", "print the vault
      status" and "how do i say 'network status' in spanish".

    Containment cannot tell "this sentence IS the request" from "this sentence
    MENTIONS the request", and every wrapper — a film, a possessive, a
    negation, a verb of transmission, a translation question — reintroduces the
    same bug one level up. So the normalized utterance must EQUAL a declared
    phrase. Anything with extra content is ambiguous by construction and is
    left to the model, which can weigh context; this layer cannot.

    ``None`` means "not confident" and the caller falls through to ordinary
    routing. There is no best-guess fallback, because answering the wrong
    question with authority is the failure mode this layer exists to avoid.
    """
    said = _normalize_utterance(query)
    if not said:
        return None
    for c in all_capabilities():
        for phrase in getattr(c, "dispatch_phrases", ()) or ():
            if said == _normalize_utterance(phrase):
                return c
    return None


def mentions_dispatch_subject(query: str) -> "Capability | None":
    """The appliance read a sentence is plainly ABOUT, by containment.

    Deliberately NOT a dispatch decision, and it must never be wired to one.
    :func:`find_for_dispatch` requires whole-utterance equality precisely
    because containment cannot tell "this sentence IS the request" from "this
    sentence MENTIONS the request" — containment ran a router scan for "how do
    i say 'network status' in spanish". That argument is unchanged.

    This answers a different and much smaller question, asked in exactly one
    place (``api_server._compound_read_clause``): *is this clause of a
    sequenced turn about the appliance at all?* Without it the second clause
    of "check whats in the pantry, then tell me the network status too" can
    find no reader and be answered out of the offline encyclopedia (LIN-bus
    wiring presented as this household's network status, made credible by the
    real pantry answer above it). A clause that names an appliance read and has
    no reader for it must say so, not borrow the library's confidence.

    Returns the LONGEST matching phrase's capability, so "what is in my vault"
    resolves to ``vault.secrets`` rather than to whichever shorter phrase of
    another capability happened to be scanned first.
    """
    said = _normalize_utterance(query)
    if not said:
        return None
    best, best_len = None, 0
    for c in all_capabilities():
        for phrase in getattr(c, "dispatch_phrases", ()) or ():
            p = _normalize_utterance(phrase)
            # Whole-word containment: "vault status" must not fire inside
            # "revault statused". Not \b — the phrases can end in punctuation-
            # free apostrophes ("what's the vault status").
            if p and re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", said):
                if len(p) > best_len:
                    best, best_len = c, len(p)
    return best


def _surfaces_sentence(cap) -> str:
    """Render a capability's surfaces as one natural clause, e.g.
    'just ask me, see it on your dashboard, or open the app'."""
    phrases = [_SURFACE_PHRASE[s] for s in live_surfaces(cap)
               if s in _SURFACE_PHRASE]
    if not phrases:
        return ""
    if len(phrases) == 1:
        return phrases[0]
    return ", ".join(phrases[:-1]) + ", or " + phrases[-1]


def reach(cap_id: str) -> str:
    """Every surface a capability is reachable through, as one sentence. The
    LLM uses this to move a capability between surfaces ("show it on my
    dashboard", "open it in the app") — they are facets of one thing."""
    c = get(cap_id)
    if not c:
        return ""
    s = _surfaces_sentence(c)
    return f"{c.label}: {s}." if s else c.label


#: A question aimed squarely at the WEB / internet tools. "what can you do"
#: says "web tools (off - Settings > Egress)", but the direct question "what
#: web tools do you have" would otherwise fall through to ZIM RAG and answer
#: with a book about classroom Web 2.0 tools, telling the user PuffPAI HAS web
#: tools and never that they are switched off. These phrasings reach the same
#: door-aware read.
WEB_TOOLS_QUESTION_RE = re.compile(
    r"\b(?:web|internet|online)\s+(?:tools?|access|search|searches)\b"
    r"|\bcan\s+you\s+(?:search|browse|look\s+(?:things\s+)?up\s+on)\s+"
    r"(?:the\s+)?(?:web|internet|online)\b"
    r"|\bcan\s+you\s+(?:go|get)\s+online\b"
    r"|\bdo\s+you\s+have\s+(?:web|internet)\s+access\b"
    r"|\bsearch\s+the\s+(?:web|internet)\b",
    re.IGNORECASE)


def describe_web_tools() -> str:
    """The web tools, and whether their egress doors are actually open.

    Claims no reach the appliance does not have. With both doors shut this says
    so plainly and names where to change it; with one open it says which is on
    and which is not. An unreadable door registry is handled inside
    ``_any_door_open``, which prefers not to invent an "off".
    """
    names = {"web_search": "web search", "web_fetch": "page fetch"}
    open_doors = [d for d in _WEB_TOOL_DOORS if _any_door_open((d,))]
    shut = [names[d] for d in _WEB_TOOL_DOORS if d not in open_doors]
    if not open_doors:
        return ("My web tools are web search and page fetch, and both are "
                "switched off right now" + EGRESS_OFF_NOTE.strip() + ". "
                "Nothing I tell you comes from the internet while they are "
                "off - I answer from the offline knowledge library on this "
                "box instead. Turn them on in Settings > Egress if you want "
                "me reaching out.")
    on = ", ".join(names[d] for d in open_doors)
    tail = (" " + " and ".join(shut) + " is off" + EGRESS_OFF_NOTE.rstrip()
            + ".") if shut else ""
    return "My web tools are {}, switched on right now.{}".format(on, tail)


def describe_for(query: str) -> str:
    """Introspection entry point for a free-text ask ("what can you do",
    "what can you access in my vault"). Resolves the area the user named, if
    any, then returns the spoken description."""
    if query and WEB_TOOLS_QUESTION_RE.search(query):
        return describe_web_tools()
    dom = _domain_of_query(query) if query else None
    return describe(dom, for_voice=True)


#: Areas of the overview that cannot work at all with their egress doors shut,
#: and the doors they need. An area counts as reachable when ANY of its doors is
#: open — "weather and alerts" is still true with air_quality off.
#:
#: "what can you do" must not advertise "web tools" while BOTH web doors are
#: shut and chat_allow_egress is false. Advertising a
#: capability the appliance has deliberately closed is the same class of
#: dishonesty as claiming an action that did not happen — the owner turned it
#: off on purpose and deserves to be told it is off, not sold it again.
_DOMAIN_DOORS = {
    "weather": ("weather", "air_quality"),
    "news": ("news_rss",),
}

#: The same table one level down: capabilities whose SEARCH half needs a door,
#: inside an area that is otherwise reachable without one. "streaming" is not in
#: `_DOMAIN_DOORS` on purpose — Spotify and the other streaming services do not go through these
#: doors, so marking the whole area off would trade one wrong answer for
#: another.
_CAPABILITY_DOORS = {
    "streaming.radio": ("radio_browser",),
}

#: What still works with those doors shut — said out loud so "off" does not
#: read as "gone".
_CAPABILITY_DOOR_NOTE = {
    "streaming.radio": "Saved stations still play.",
}

#: Pieces of a ``data_source`` string that each depend on their own door.
#: TuneIn has its own default-CLOSED door, while the registry once
#: still described the row as "radio-browser / TuneIn" unconditionally — a
#: source the stock box cannot reach, advertised in the one answer that must not
#: over-claim. Each name is marked with the same ``EGRESS_OFF_NOTE`` the areas
#: use, so the owner is told which half is shut and where the switch is.
_SOURCE_DOORS = {
    "streaming.radio": (("radio-browser", "radio_browser"),
                        ("TuneIn", "tunein")),
}

#: The doors behind the "web tools" clause of the overview tail.
_WEB_TOOL_DOORS = ("web_search", "web_fetch")

#: What a shut door reads as in a spoken list.
EGRESS_OFF_NOTE = " (off — Settings > Egress)"


def _chat_egress_on() -> bool:
    """Can the turn the owner is SPEAKING go online at all?

    "what can you do" must not offer "web tools" with no marker while chat
    turns are not allowed out. A door that is open for a
    planned run is not reach this conversation has, and the overview is being
    read to somebody who is in a conversation. Both halves are required, the
    same AND ``egress_policy.chat_egress_allowed`` applies at the gate.

    The DOOR half is asked through ``egress_policy.chat_egress_allowed``, which
    is the gate a spoken turn actually passes through: asking the doors
    separately would let the overview and the gate disagree about the same
    turn. An unreadable policy still returns True, matching ``_any_door_open``:
    this function exists to stop OVERSTATING reach, not to invent an "off".
    """
    try:
        from puffpai.security import egress_policy
        if not egress_policy.enabled("chat_allow_egress"):
            return False
        for door in _WEB_TOOL_DOORS:
            allowed, _reason = egress_policy.chat_egress_allowed(door)
            if allowed:
                return True
        return False
    except Exception:             # pragma: no cover - registry ships with it
        return True


def _any_door_open(door_ids) -> bool:
    """True when at least one of *door_ids* is open right now.

    An unreadable door registry returns True: this function exists to stop
    OVERSTATING what PuffPAI can reach, and marking a working area "off"
    because the registry could not be imported would be its own wrong answer.
    """
    if not door_ids:
        return True
    try:
        from puffpai.security.egress_doors import egress_allowed
    except Exception:  # pragma: no cover - registry always ships with it
        return True
    for door_id in door_ids:
        try:
            allowed, _reason = egress_allowed(door_id)
        except Exception:
            return True
        if allowed:
            return True
    return False


def _area_note(domain: str) -> str:
    """``""`` when the area is reachable, else the "off" marker."""
    return "" if _any_door_open(_DOMAIN_DOORS.get(domain, ())) else EGRESS_OFF_NOTE


def _cap_note(cap_id: str) -> str:
    """``""`` when this capability's own doors let it work, else the marker."""
    return ("" if _any_door_open(_CAPABILITY_DOORS.get(cap_id, ()))
            else EGRESS_OFF_NOTE)


def live_data_source(cap) -> str:
    """A capability's data source with each shut-door half marked.

    ``radio-browser / TuneIn`` reads plainly with both doors open, and names the
    closed half — never silently — when one is shut. Unlisted capabilities get
    their declared string back untouched.
    """
    pairs = _SOURCE_DOORS.get(getattr(cap, "id", ""), ())
    if not pairs:
        return cap.data_source
    return " / ".join(
        name + ("" if _any_door_open((door,)) else EGRESS_OFF_NOTE)
        for name, door in pairs)


def describe(domain: str | None = None, for_voice: bool = True) -> str:
    """Human/spoken description of what PuffPAI can access.

    ``domain=None`` → an overview across the main domains. ``domain="home"`` →
    the detail for that area, including every surface it is reachable through
    (voice / dashboard / app), because it is ONE capability, not separate
    abilities. This is the text PuffPAI speaks for "what can you access?".
    """
    if domain:
        d = (domain or "").strip().lower()
        caps = by_domain(d)
        if not caps:
            return (f"I don't have a '{domain}' area. Ask me 'what can you "
                    f"access' for the full list.")
        head = f"For {d}{_area_note(d)}, I can help with:"
        lines = []
        for c in caps:
            # Probed, not declared: "what can you do" is the one
            # answer that must not describe a capability the box lost.
            extra = _STATUS_NOTE.get(runtime_status(c), "")
            available, reason = runtime_availability(c)
            if not available and not extra:
                # Live area, unavailable action — say which, briefly. Only the
                # first sentence: this is read aloud.
                first = reason.split(". ")[0].strip().rstrip(".")
                extra = f" ({first})" if first else " (not available right now)"
            reach_clause = _surfaces_sentence(c)
            tail = f" — {reach_clause}." if reach_clause else "."
            note = _cap_note(c.id)
            lines.append(f"  - {c.label}{note}{extra}: "
                         f"{live_data_source(c)}{tail}")
            if note and c.id in _CAPABILITY_DOOR_NOTE:
                lines[-1] += " " + _CAPABILITY_DOOR_NOTE[c.id]
        return head + "\n" + "\n".join(lines)

    # Overview — the main user-facing areas (those in _DOMAIN_NICE), kept tight
    # (a spoken turn caps ~500 chars). Niche areas are still introspectable via
    # "what can you do with my <area>".
    live_areas, planned_areas = [], []
    for d in DOMAIN_ORDER:
        if d not in _DOMAIN_NICE:
            continue
        caps = by_domain(d)
        if not caps:
            continue
        nice = _DOMAIN_NICE[d]
        if all(runtime_status(c) == PLANNED for c in caps):
            planned_areas.append(nice)
        else:
            live_areas.append(nice + _area_note(d))
    intro = ("I'm one system — reach me by voice, on the dashboard, or in the "
             "app, not separate apps. I handle: ")
    planned = (f" Not built yet: {', '.join(planned_areas)}."
               if planned_areas else "")
    # "web tools" is only true while a web door is open — see _DOMAIN_DOORS.
    extras = ["a calculator", "health checks", "trivia"]
    if _chat_egress_on():
        extras.append("web tools")
    else:
        extras.append("web tools" + EGRESS_OFF_NOTE)
    tail = (f" I've also got {', '.join(extras[:-1])}, and {extras[-1]}. "
            "Ask me about any area — like your home — for the details.")
    return intro + "; ".join(live_areas) + "." + planned + tail


def validate(live_tool_names) -> list:
    """Return a list of problems: declared tools that don't exist in the live
    tool set, and agents not on the roster. Empty list == registry is honest.
    Called by the test suite so a renamed/removed tool fails a test instead of
    silently lying to the user.
    """
    problems: list = []
    names = set(live_tool_names or [])
    for c in all_capabilities():
        for t in c.all_tools:
            if t not in names:
                problems.append(f"{c.id}: tool '{t}' not in live tool set")
    try:
        from puffpai.agents.registry import ROSTER
        roster = {e.name for e in ROSTER}
        for c in all_capabilities():
            if c.agent and c.agent not in roster:
                problems.append(f"{c.id}: agent '{c.agent}' not on roster")
    except Exception:
        pass
    return problems


__all__ = [
    "Capability", "CAPABILITIES", "READ", "WRITE", "READWRITE",
    "LIVE", "NEEDS_AUTH", "OFFLINE", "PLANNED",
    "all_capabilities", "get", "domains", "by_domain", "authority_for",
    "clear_skill_overlay", "set_skill_overlay", "skill_capabilities",
    "tools_for_query", "find", "reach", "describe", "describe_for", "validate",
    "VOICE", "CHAT", "DASHBOARD", "APP",
]
