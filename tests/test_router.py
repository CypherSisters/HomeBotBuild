"""Tests for the unified message router."""

import pytest
from puffpai.router import (
    route_message, RouteTarget, should_use_home_tools,
    detect_needs_tools, get_route_category, detect_app_launch,
)


class TestRouteMessage:
    def test_home_assistant_keywords(self):
        assert route_message("turn on the lights") == RouteTarget.AGENT
        assert route_message("set brightness to 50") == RouteTarget.AGENT
        assert route_message("discover devices on my network") == RouteTarget.AGENT

    def test_security_keywords(self):
        assert route_message("block device 192.168.1.5") == RouteTarget.AGENT
        assert route_message("run a network scan") == RouteTarget.AGENT
        assert route_message("enable lockdown mode") == RouteTarget.AGENT

    def test_web_search_keywords(self):
        assert route_message("search for python tutorials") == RouteTarget.AGENT
        assert route_message("what is the latest news?") == RouteTarget.AGENT
        assert route_message("look up the weather") == RouteTarget.AGENT

    def test_prep_keywords(self):
        assert route_message("check my emergency supplies") == RouteTarget.AGENT
        assert route_message("any weather alerts today?") == RouteTarget.AGENT

    def test_prep_quantity_questions_reach_the_agent(self):
        # Benchmark 2.7: this exact phrasing fell through to RAG, which
        # has no tools, so prep_days_remaining could never fire.
        assert route_message("How many days of food do we have?") == \
            RouteTarget.AGENT
        assert route_message("do we have enough water?") == RouteTarget.AGENT
        assert route_message("how much food is left in storage?") == \
            RouteTarget.AGENT

    def test_action_keywords(self):
        assert route_message("calculate 5 * 3") == RouteTarget.AGENT
        assert route_message("write file notes.txt") == RouteTarget.AGENT

    def test_library_query(self):
        assert route_message("list indexed libraries") == RouteTarget.LIBRARY
        assert route_message("what knowledge base collections do you have?") == RouteTarget.LIBRARY
        assert route_message("how many wikipedia databases are there?") == RouteTarget.LIBRARY

    def test_library_query_natural_phrasings(self):
        """Expanded library query detection — natural language."""
        assert route_message("tell me about your 54 knowledge libraries") == RouteTarget.LIBRARY
        assert route_message("show me what libraries you have") == RouteTarget.LIBRARY
        assert route_message("which databases are available?") == RouteTarget.LIBRARY
        assert route_message("describe the indexed collections") == RouteTarget.LIBRARY
        assert route_message("do you have access to Wikipedia?") == RouteTarget.LIBRARY

    def test_briefing_keywords(self):
        assert route_message("give me my daily briefing") == RouteTarget.BRIEFING
        assert route_message("morning update please") == RouteTarget.BRIEFING
        assert route_message("catch me up on everything") == RouteTarget.BRIEFING
        assert route_message("what did i miss?") == RouteTarget.BRIEFING
        assert route_message("brief me") == RouteTarget.BRIEFING

    def test_app_keywords(self):
        # AlarmFlow
        assert route_message("set a timer for 5 minutes") == RouteTarget.AGENT
        assert route_message("cancel alarm") == RouteTarget.AGENT
        # Calendar
        assert route_message("add to calendar tomorrow") == RouteTarget.AGENT
        assert route_message("what is on my schedule") == RouteTarget.AGENT
        # FreshTable
        assert route_message("add to shopping list") == RouteTarget.AGENT
        assert route_message("what's in my pantry?") == RouteTarget.AGENT
        # FitFam
        assert route_message("log workout today") == RouteTarget.AGENT
        # Trivia (stats query — no launch verb, goes to AGENT)
        assert route_message("trivia stats") == RouteTarget.AGENT

    def test_app_launch_trivia(self):
        """Play/open/start trivia → APP_LAUNCH (bypasses LLM)."""
        assert route_message("I want to play a trivia game") == RouteTarget.APP_LAUNCH
        assert route_message("play trivia") == RouteTarget.APP_LAUNCH
        assert route_message("start a quiz") == RouteTarget.APP_LAUNCH
        assert route_message("let's do trivia") == RouteTarget.APP_LAUNCH
        assert route_message("I'd like to play a mid level game about music") == RouteTarget.APP_LAUNCH

    def test_app_launch_other_apps(self):
        """Open/launch non-trivia apps → APP_LAUNCH."""
        assert route_message("open the calendar") == RouteTarget.APP_LAUNCH
        assert route_message("launch the timer app") == RouteTarget.APP_LAUNCH
        assert route_message("open fitfam") == RouteTarget.APP_LAUNCH
        assert route_message("open freshtable") == RouteTarget.APP_LAUNCH

    def test_app_data_ops_still_agent(self):
        """Data operations (add/list) still go to AGENT, not APP_LAUNCH."""
        assert route_message("add a task to the calendar for tomorrow") == RouteTarget.AGENT
        assert route_message("set a timer for 5 minutes") == RouteTarget.AGENT
        assert route_message("what is on my schedule for today") == RouteTarget.AGENT
        assert route_message("add to shopping list: milk") == RouteTarget.AGENT

    def test_planful_workflow_creation_reaches_agent(self):
        """Keyword-free 'build me a workflow/routine' phrasings must reach the
        agent team (planning loop), not fall through to the tool-less RAG path.
        """
        assert route_message(
            "create a workflow that greets my guests") == RouteTarget.AGENT
        assert route_message(
            "build me a routine for the afternoon") == RouteTarget.AGENT
        assert route_message(
            "compose a sequence of actions to welcome visitors") == \
            RouteTarget.AGENT
        assert route_message("set up my morning routine") == RouteTarget.AGENT
        assert route_message(
            "put together a routine for the evening") == RouteTarget.AGENT
        assert route_message(
            "organize a sequence of chores for the weekend") == \
            RouteTarget.AGENT

    def test_planning_detector_does_not_hijack_knowledge_questions(self):
        """Conservative guard: a plan object OR a planful verb alone is a
        knowledge question and stays on RAG. Asserted at the detector so the
        check is not confounded by unrelated keyword matches (e.g. 'meeting').
        """
        from puffpai.router import _looks_like_planning_request
        # neither verb nor object
        assert _looks_like_planning_request("what's the capital of france") \
            is False
        # 'plan' as a noun in a question — no construction verb, no object
        assert _looks_like_planning_request(
            "what is the plan for the meeting") is False
        # object but no construction verb
        assert _looks_like_planning_request("tell me about workflows") is False
        # construction verb but no plan object
        assert _looks_like_planning_request("build a birdhouse") is False
        # and the full router keeps the clean ones on RAG
        assert route_message("what's the capital of france") == RouteTarget.RAG
        assert route_message("tell me about workflows") == RouteTarget.RAG

    def test_general_fallback(self):
        assert route_message("tell me about photosynthesis") == RouteTarget.RAG
        assert route_message("hello how are you?") == RouteTarget.RAG

    def test_mode_override_home(self):
        assert route_message("anything", mode="home") == RouteTarget.HOME

    def test_mode_override_general(self):
        assert route_message("turn on the lights", mode="general") == RouteTarget.RAG

    def test_empty_message(self):
        assert route_message("") == RouteTarget.RAG

    def test_none_message(self):
        assert route_message(None) == RouteTarget.RAG

    def test_case_insensitive(self):
        assert route_message("TURN ON THE LIGHTS") == RouteTarget.AGENT
        assert route_message("Search For Python") == RouteTarget.AGENT


class TestHouseStateQuestions:
    """Reading the house reaches the agent; reading the world does not.

    "what is the temperature in the living room?" must not classify as
    RAG, or it never reaches the agent team, and the tool-less RAG path
    would answer that it has no way to read the thermostat, false, the house
    has two live ``climate.*`` entities the Concierge reads correctly.
    """

    # (utterance, expected route) — the whole discriminator in one table.
    TABLE = [
        # ── house STATE questions (the bug) → AGENT ──
        ("what is the temperature in the living room?", RouteTarget.AGENT),
        ("what's the temp in the bedroom", RouteTarget.AGENT),
        ("how warm is it in here", RouteTarget.AGENT),
        ("what's the humidity in the basement", RouteTarget.AGENT),
        ("is the porch light on?", RouteTarget.AGENT),
        ("are the kitchen lights off", RouteTarget.AGENT),
        ("is the front door locked", RouteTarget.AGENT),
        ("is the garage door open", RouteTarget.AGENT),
        ("did i leave the garage door open", RouteTarget.AGENT),
        ("what is the state of the back door", RouteTarget.AGENT),
        ("how many lights are on in the house", RouteTarget.AGENT),
        # ── house COMMANDS (already worked) → no regression ──
        ("turn on the porch light", RouteTarget.AGENT),
        ("turn off the kitchen light", RouteTarget.AGENT),
        ("dim the kitchen light to 30%", RouteTarget.AGENT),
        ("set the thermostat to 68 degrees", RouteTarget.AGENT),
        # ── knowledge questions → still RAG ──
        ("what is the average temperature in texas in august", RouteTarget.RAG),
        ("at what temperature does water boil", RouteTarget.RAG),
        ("what is normal body temperature", RouteTarget.RAG),
        ("what's the temperature in paris", RouteTarget.RAG),
        ("how do i purify water", RouteTarget.RAG),
        ("what bands does the kaito ka700 cover", RouteTarget.RAG),
        ("what does the manual say about the generator", RouteTarget.RAG),
        ("tell me about photosynthesis", RouteTarget.RAG),
        ("is the water safe to drink", RouteTarget.RAG),
        # ── weather: unchanged, and never a house-state question ──
        ("what's the weather", RouteTarget.AGENT),
        ("what's the forecast for this weekend", RouteTarget.AGENT),
        ("will it rain tomorrow", RouteTarget.RAG),
        # ── ambiguous, documented choice: bare reading = the house ──
        ("what's the temperature", RouteTarget.AGENT),
    ]

    @pytest.mark.parametrize("text,expected", TABLE)
    def test_routing_table(self, text, expected):
        assert route_message(text) == expected, \
            f"{text!r} -> {route_message(text).value}"

    def test_bare_temperature_prefers_the_house(self):
        """Documented ambiguity call.

        "What's the temperature?" names no place. A house assistant that
        answers about the house is right more often, and the knowledge path
        stays one rephrase away ("what's the temperature in Denver").
        """
        assert route_message("what's the temperature") == RouteTarget.AGENT
        assert route_message("what's the temperature in denver") == RouteTarget.RAG

    def test_weather_keeps_its_existing_route(self):
        """Weather is web-search, not house-state, even though HA has weather.*."""
        assert get_route_category("what's the weather") == "web_search"
        assert get_route_category("what's the forecast") == "web_search"

    def test_house_state_category_is_home(self):
        assert get_route_category("is the porch light on?") == "home"
        assert get_route_category(
            "what is the temperature in the living room?") == "home"

    def test_knowledge_questions_keep_the_rag_category(self):
        for text, expected in self.TABLE:
            if expected is RouteTarget.RAG:
                assert get_route_category(text) != "home", text


class TestHouseReadsPhrasedAsInstructions:
    """Asking for a reading in the imperative is still asking for a reading.

    Every signal in TestHouseStateQuestions is interrogative. Live on the Orin,
    the same requests phrased as instructions classified as RAG and came back
    "I'm not able to read the temperature sensor or inventory devices directly
    from here" — with ZIM knowledge sources attached. The sources are the tell:
    a degraded *house* answer that cites the library never reached the team.

    Compound requests paid for it twice. "check the temperature in the living
    room and then tell me what devices are here" never reached the Concierge,
    so the Phase-4 planning loop never saw it — though the same goal sent
    straight to the Concierge finishes in ~5s with real data.
    """

    # (utterance, expected route)
    TABLE = [
        # ── the reported misroutes → AGENT ──
        ("check the temperature in the living room and then tell me "
         "what devices are here", RouteTarget.AGENT),
        ("what devices are here", RouteTarget.AGENT),
        ("read the living room temperature and then show me every device",
         RouteTarget.AGENT),
        ("list my devices and then check the living room temperature",
         RouteTarget.AGENT),
        # ── the phrasing that already worked → no regression ──
        ("what is the temperature in the living room and then list my devices",
         RouteTarget.AGENT),
        # ── same shapes, single step ──
        ("check the temperature in the living room", RouteTarget.AGENT),
        ("read the humidity in the basement", RouteTarget.AGENT),
        ("show me the thermostat status", RouteTarget.AGENT),
        ("list my devices", RouteTarget.AGENT),
        ("show me every device", RouteTarget.AGENT),
        ("what devices do we have", RouteTarget.AGENT),
        ("how many devices are there", RouteTarget.AGENT),
        # ── an inspect verb with nothing readable is a topic, not a reading ──
        ("tell me about the history of the kitchen", RouteTarget.RAG),
        ("read me the chapter on basement waterproofing", RouteTarget.RAG),
        ("show me a recipe for bread", RouteTarget.AGENT),  # documents, not house
        # ── something readable with no house to read it from ──
        ("check the status of my order", RouteTarget.RAG),
        ("what is the state of the economy", RouteTarget.RAG),
        # ── "devices" about the world, not this house ──
        ("what devices did edison invent", RouteTarget.RAG),
        ("which devices came before the transistor", RouteTarget.RAG),
        # ── the world vetoes still win over an inspect verb ──
        ("check the average temperature in texas in august", RouteTarget.RAG),
        ("tell me the temperature at which water boils", RouteTarget.RAG),
        ("check the temperature in paris", RouteTarget.RAG),
    ]

    @pytest.mark.parametrize("text,expected", TABLE)
    def test_routing_table(self, text, expected):
        assert route_message(text) == expected, \
            f"{text!r} -> {route_message(text).value}"

    def test_all_three_signals_are_required_for_an_imperative_read(self):
        """Verb AND readable AND a house anchor — any two of the three is a
        knowledge question, which is why the branch does not settle for them."""
        # verb + readable, no house         → the world
        assert route_message("check the state of the union") == RouteTarget.RAG
        # verb + house, nothing readable    → a topic
        assert route_message("tell me about the garage") == RouteTarget.RAG
        # readable + house, no verb         → already covered, or RAG
        assert route_message("basement humidity in old houses") == RouteTarget.RAG

    def test_device_inventory_needs_an_ours_anchor(self):
        """"my"/"our"/"here" is what separates this house's inventory from a
        general question that happens to use the word "devices"."""
        assert route_message("what devices are here") == RouteTarget.AGENT
        assert route_message("what devices do i have") == RouteTarget.AGENT
        assert route_message("what devices did edison invent") == RouteTarget.RAG

    def test_category_agrees_with_the_route(self):
        """get_route_category() mirrors route_message() — never the reverse."""
        for text, expected in self.TABLE:
            if expected is RouteTarget.AGENT and "recipe" not in text:
                assert get_route_category(text) == "home", text


class TestLegacyCompat:
    def test_should_use_home_tools_true(self):
        assert should_use_home_tools("turn on the lights") is True

    def test_should_use_home_tools_false(self):
        assert should_use_home_tools("tell me about python") is False

    def test_should_use_home_tools_mode_home(self):
        assert should_use_home_tools("anything", mode="home") is True

    def test_should_use_home_tools_mode_general(self):
        assert should_use_home_tools("turn on lights", mode="general") is False

    def test_should_use_home_tools_briefing(self):
        assert should_use_home_tools("give me my daily briefing") is True

    def test_detect_needs_tools(self):
        assert detect_needs_tools("block device 10.0.0.1") is True
        assert detect_needs_tools("hello") is False


class TestDetectAppLaunch:
    """detect_app_launch() returns app_id for launch-intent messages."""

    def test_trivia_variants(self):
        assert detect_app_launch("play trivia") == "trivia"
        assert detect_app_launch("start a quiz") == "trivia"
        assert detect_app_launch("let's do brain training") == "trivia"

    def test_calendar(self):
        assert detect_app_launch("open the calendar") == "calendar"
        assert detect_app_launch("pull up my schedule") == "calendar"

    def test_no_launch_for_data_ops(self):
        assert detect_app_launch("add a task to the calendar") is None
        assert detect_app_launch("set a timer for 5 minutes") is None

    def test_no_launch_for_general(self):
        assert detect_app_launch("tell me about python") is None
        assert detect_app_launch("hello") is None


class TestRouteCategory:
    def test_briefing_category(self):
        assert get_route_category("daily briefing please") == "briefing"
        assert get_route_category("catch me up") == "briefing"

    def test_home_category(self):
        assert get_route_category("turn on the lights") == "home"

    def test_security_category(self):
        assert get_route_category("block device") == "security"

    def test_prep_category(self):
        assert get_route_category("check emergency supplies") == "prep"

    def test_web_search_category(self):
        assert get_route_category("search for news") == "web_search"

    def test_app_category(self):
        assert get_route_category("set a timer") == "app"
        assert get_route_category("add to calendar") == "app"
        assert get_route_category("log workout") == "app"

    def test_action_category(self):
        assert get_route_category("calculate 2+2") == "action"

    def test_library_category(self):
        assert get_route_category("list indexed libraries") == "library"

    def test_rag_category(self):
        assert get_route_category("explain quantum physics") == "rag"


# ── pairing phrasings reach the agent (found live: they fell to RAG) ─

import pytest as _pytest
from puffpai.router import route_message as _rm, RouteTarget as _RT


@_pytest.mark.parametrize("msg", [
    "pair my new light bulbs",
    "can you pair the bulbs I just plugged in",
    "put the hub in pairing mode",
    "pairing status",
    "start pairing",
    "did you find any new devices?",
])
def test_pairing_phrasings_route_agent(msg):
    assert _rm(msg) == _RT.AGENT, msg


@_pytest.mark.parametrize("msg", [
    "repair my door",
    "compare my bulbs to the old ones",
    "how do i repair a lamp cord",
    "i bought a pair of pliers",
    "what impairs sleep quality",
])
def test_pairing_detector_leaves_prose_alone(msg):
    from puffpai.router import _looks_like_pairing_request
    assert not _looks_like_pairing_request(msg.lower()), msg
