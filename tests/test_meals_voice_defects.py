"""Meal-plan writes by voice, and cut-off voice fragments.

DEFECT A: a meal plan written from a CONVERSATION.

    "every week." (a cut sentence)
    "Add both of those salmon recipes to the dinner meals for this week."
        -> asks which days
    "Let's do Wednesday for the first salmon recipe and Thursday for the
     second the herb crusted one." -> asks to confirm
    "Yes please, that's perfect."
        -> must write the plan, never "I don't have a tool in this
           environment that can actually create or schedule recipes inside
           FreshTable."

``capability_registry`` DOES declare ``food.mealplan`` with ``meal_set``, but
``tools_for_query`` binds from the CURRENT utterance, and "Yes please, that's
perfect." carries no food keyword, so on the agent route the bind falls to the
three-tool safety floor in ``_filter_tools_by_intent``. A clarify -> confirm
flow cannot complete there by construction, so it is handled by a fastpath
that reads the conversation.

DEFECT B: utterance fragmenting.
``PUFF_SILENCE_HANG`` is 1.0 s, so a pause after a fronted time phrase can end
the utterance and ship a fragment such as "every week." or "every Tuesday." as
a whole turn. Answered as-is, each gets an encyclopedia answer while the person
still has the rest of the sentence to say.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import fragments  # noqa: E402
from puffpai.agents import meal_writes as mw  # noqa: E402
import puffpai.api_server as api  # noqa: E402


# ── the conversation under test ────────────────────────────────────────────
SALMON_1 = (
    "Here's a quick, crowd-pleasing Pan-Seared Salmon with Lemon-Butter Sauce "
    "that'll bring the ocean to your table.\n"
    "Ingredients\n"
    "- 4 salmon fillets\n"
    "- 2 tbsp butter\n"
    "- 1 lemon\n"
    "Instructions\n"
    "1. Pat the fillets dry and season.\n"
    "2. Sear skin-side down for 4 minutes.\n"
)

SALMON_2 = (
    "Here's another tasty option you might like: Herb-Crusted Salmon with "
    "Dill.\n"
    "Ingredients\n"
    "- 4 salmon fillets\n"
    "- 1 bunch fresh dill\n"
    "- 3 tbsp panko\n"
    "Instructions\n"
    "1. Preheat the oven to 400F.\n"
    "2. Press the herb crust onto each fillet and bake 12 minutes.\n"
)

HISTORY = [
    {"role": "user", "content": "Can I get a salmon recipe?"},
    {"role": "assistant", "content": SALMON_1},
    {"role": "user", "content": "every week."},
    {"role": "assistant", "content": SALMON_2},
]

ADD_BOTH = "Add both of those salmon recipes to the dinner meals for this week."
NAME_DAYS = ("Let's do Wednesday for the first salmon recipe and Thursday for "
             "the second the herb crusted one.")
CONFIRM = "Yes please, that's perfect."


class FakeAppData:
    """Just enough FreshTable to observe what a turn WROTE."""

    def __init__(self):
        self.recipes = []
        self.plan = []

    def _load(self, app_id):
        return {"recipes": self.recipes}

    def fresh_create_recipe(self, name, ingredients, instructions):
        self.recipes.append({"id": f"r{len(self.recipes) + 1}", "name": name,
                             "ingredients": ", ".join(ingredients),
                             "instructions": instructions})
        return f"Recipe created: {name}."

    def fresh_set_meal(self, day, meal, dish, notes=None, recipe_id=None):
        self.plan = [e for e in self.plan
                     if not (e["day"] == day and e["meal"] == meal)]
        self.plan.append({"day": day, "meal": meal, "dish": dish,
                          "notes": notes, "recipeId": recipe_id})
        return f"Planned {meal} on {day.capitalize()}: {dish}."


@pytest.fixture(autouse=True)
def _clean_pending():
    mw.clear_pending()
    yield
    mw.clear_pending()


@pytest.fixture
def app():
    return FakeAppData()


# ══════════════════════════════════════════════════════════════════════════
# DEFECT A · the recipes are found in the conversation
# ══════════════════════════════════════════════════════════════════════════

def test_both_salmon_recipes_are_recovered_from_the_conversation():
    found = mw.extract_recipes(HISTORY)
    titles = [r["title"] for r in found]
    assert titles == ["Pan-Seared Salmon with Lemon-Butter Sauce",
                      "Herb-Crusted Salmon with Dill"]


def test_a_recovered_recipe_carries_its_body_not_just_a_name():
    first = mw.extract_recipes(HISTORY)[0]
    assert "4 salmon fillets" in first["ingredients"]
    assert "Sear skin-side down" in first["instructions"]


def test_chatter_with_no_food_in_it_offers_no_recipe():
    assert mw.extract_recipes([
        {"role": "assistant", "content": "One of the best parts of Seattle is "
                                         "Pike Place Market."}]) == []


# ── reference resolution, in spoken words ──────────────────────────────────

def test_both_of_those_salmon_recipes_resolves_to_two():
    found = mw.extract_recipes(HISTORY)
    assert len(mw.resolve_targets(ADD_BOTH, found)) == 2


def test_the_herb_crusted_one_resolves_to_the_second():
    found = mw.extract_recipes(HISTORY)
    got = mw.resolve_targets("put the herb-crusted one on Thursday", found)
    assert [r["title"] for r in got] == ["Herb-Crusted Salmon with Dill"]


def test_that_salmon_recipe_resolves_to_the_most_recent_one():
    found = mw.extract_recipes(HISTORY)
    got = mw.resolve_targets("add that salmon recipe to Wednesday's dinner",
                             found)
    assert len(got) == 1


def test_days_are_read_in_the_order_they_were_spoken():
    assert mw.parse_days(NAME_DAYS) == ["wednesday", "thursday"]
    assert mw.parse_days(
        "add both of those to dinner this week, Wednesday and Thursday"
    ) == ["wednesday", "thursday"]


def test_a_turn_that_names_no_meal_type_means_dinner():
    assert mw.parse_meal_type("put the herb-crusted one on Thursday") == "dinner"
    assert mw.parse_meal_type("add that to Friday lunch") == "lunch"


# ══════════════════════════════════════════════════════════════════════════
# DEFECT A · the WRITE
# ══════════════════════════════════════════════════════════════════════════

def test_add_that_salmon_recipe_to_wednesdays_dinner_writes_the_plan(app,
                                                                    monkeypatch):
    monkeypatch.setattr(mw, "commit", _committing(app))
    reply = mw.handle("add that salmon recipe to Wednesday's dinner", HISTORY)
    assert app.plan and app.plan[0]["day"] == "wednesday"
    assert app.plan[0]["meal"] == "dinner"
    assert "Salmon" in app.plan[0]["dish"]
    assert "Wednesday" in reply and "Salmon" in reply


def test_the_scheduled_slot_links_a_real_recipe_not_just_a_title(app,
                                                                 monkeypatch):
    monkeypatch.setattr(mw, "commit", _committing(app))
    mw.handle("add that salmon recipe to Wednesday's dinner", HISTORY)
    assert app.recipes, "the recipe body from the conversation was dropped"
    assert app.plan[0]["recipeId"] == app.recipes[0]["id"]
    assert "salmon fillets" in app.recipes[0]["ingredients"]
    assert "Sear skin-side down" in app.recipes[0]["instructions"]
    assert "salmon fillets" in (app.plan[0]["notes"] or "")


def test_both_recipes_and_two_days_in_one_turn_write_both_slots(app,
                                                                monkeypatch):
    monkeypatch.setattr(mw, "commit", _committing(app))
    reply = mw.handle(
        "add both of those to dinner this week, Wednesday and Thursday",
        HISTORY)
    got = {(e["day"], e["dish"]) for e in app.plan}
    assert got == {("wednesday", "Pan-Seared Salmon with Lemon-Butter Sauce"),
                   ("thursday", "Herb-Crusted Salmon with Dill")}
    assert "Wednesday" in reply and "Thursday" in reply


def test_put_the_herb_crusted_one_on_thursday_writes_thursday(app, monkeypatch):
    monkeypatch.setattr(mw, "commit", _committing(app))
    mw.handle("put the herb-crusted one on Thursday", HISTORY)
    assert [(e["day"], e["dish"]) for e in app.plan] == [
        ("thursday", "Herb-Crusted Salmon with Dill")]


# ══════════════════════════════════════════════════════════════════════════
# DEFECT A · the multi-turn flow
# ══════════════════════════════════════════════════════════════════════════

def test_the_three_turn_exchange_ends_in_a_write(app, monkeypatch):
    monkeypatch.setattr(mw, "commit", _committing(app))

    ask = mw.handle(ADD_BOTH, HISTORY)
    assert ask and "which day" in ask.lower()
    assert app.plan == [], "nothing may be written before the days are known"

    done = mw.handle(NAME_DAYS, HISTORY)
    assert [(e["day"], e["dish"]) for e in app.plan] == [
        ("wednesday", "Pan-Seared Salmon with Lemon-Butter Sauce"),
        ("thursday", "Herb-Crusted Salmon with Dill")]
    assert "Wednesday" in done and "Thursday" in done


def test_a_bare_yes_after_the_days_are_known_completes_the_write(app,
                                                                 monkeypatch):
    monkeypatch.setattr(mw, "commit", _committing(app))
    mw.set_pending(mw.extract_recipes(HISTORY), "dinner",
                   ["wednesday", "thursday"])
    reply = mw.handle(CONFIRM, HISTORY)
    assert len(app.plan) == 2
    assert "don't have a tool" not in reply.lower()
    assert "Saved" in reply


def test_no_after_a_clarify_writes_nothing(app, monkeypatch):
    monkeypatch.setattr(mw, "commit", _committing(app))
    mw.set_pending(mw.extract_recipes(HISTORY), "dinner", ["wednesday"])
    reply = mw.handle("no, never mind", HISTORY)
    assert app.plan == []
    assert "nothing saved" in reply.lower()
    assert mw.get_pending() is None


def test_a_stale_pending_request_expires_rather_than_being_confirmed(
        app, monkeypatch):
    monkeypatch.setattr(mw, "commit", _committing(app))
    mw.set_pending(mw.extract_recipes(HISTORY), "dinner", ["wednesday"])
    monkeypatch.setattr(mw, "PENDING_TTL", -1)
    assert mw.handle(CONFIRM, HISTORY) is None
    assert app.plan == []


# ══════════════════════════════════════════════════════════════════════════
# DEFECT A · negatives — the read stays a read
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("phrase", [
    "what's for dinner",
    "what's for dinner tonight",
    "show me the meal plan",
    "what's on the meal plan for Wednesday",
    "plan this week's meals",
    "tell me what we're having for dinner Thursday",
])
def test_reads_and_the_week_planner_are_not_claimed(phrase, app, monkeypatch):
    monkeypatch.setattr(mw, "commit", _committing(app))
    assert mw.handle(phrase, HISTORY) is None
    assert app.plan == []


def test_an_add_with_no_recipe_in_the_conversation_is_declined(app,
                                                               monkeypatch):
    monkeypatch.setattr(mw, "commit", _committing(app))
    assert mw.handle("add that salmon recipe to Wednesday's dinner", []) is None
    assert app.plan == []


# ── the api_server hook ────────────────────────────────────────────────────

def test_the_fastpath_is_wired_into_the_instant_answer_layer(monkeypatch):
    """The hook must be reachable from BOTH pipelines, with history."""
    seen = {}

    def _fake(user_message, conversation_history=None):
        seen["msg"] = user_message
        seen["history"] = conversation_history
        return "Saved to the FreshTable meal plan:\n  - Wednesday dinner: X"

    monkeypatch.setattr(api, "_try_meal_add", _fake)
    out = api._try_instant_answer(ADD_BOTH, conversation_history=HISTORY)
    assert out and out.startswith("Saved to the FreshTable meal plan")
    assert seen["msg"] == ADD_BOTH
    assert seen["history"] is HISTORY


def test_the_hook_declines_when_freshtable_is_not_installed(monkeypatch):
    monkeypatch.setattr(api, "_app_installed", lambda app_id: False)
    assert api._try_meal_add(ADD_BOTH, HISTORY) is None


# ══════════════════════════════════════════════════════════════════════════
# DEFECT B · the fragment rule
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("fragment", [
    "every week.",
    "every Tuesday.",
    "every week",
    "and then.",
    "at 6.15.",
    "Wednesday morning.",
    "every weekday.",
])
def test_a_cut_sentence_is_recognised_as_a_fragment(fragment):
    assert fragments.is_fragment(fragment)


@pytest.mark.parametrize("command", [
    "stop", "next", "louder", "quieter", "yes", "no", "pause", "skip",
    "mute", "cancel", "snooze", "repeat", "volume up", "turn it down",
    "what time is it", "lights on", "play music", "set a timer",
    "turn off the lights", "add milk", "what's for dinner",
    "is the door locked", "tomorrow's weather", "how are you",
    "yes please", "go ahead",
])
def test_real_short_commands_are_never_treated_as_fragments(command):
    assert not fragments.is_fragment(command)


def test_the_full_sentence_is_not_a_fragment():
    assert not fragments.is_fragment(
        "Every Monday, Tuesday, Thursday, and Friday morning at 6.15, "
        "I have to take the kids to soccer practice.")


def test_the_ask_back_quotes_exactly_what_landed():
    reply = fragments.ask_again("every Tuesday.")
    assert 'every Tuesday' in reply
    assert reply.rstrip().endswith("?")


def test_the_tail_is_stitched_onto_the_head():
    assert fragments.join_extension(
        "every Tuesday.", "and Thursday I take the kids to soccer practice"
    ) == "every Tuesday and Thursday I take the kids to soccer practice"


def test_nothing_more_said_leaves_the_words_alone():
    assert fragments.join_extension("every Tuesday.", "") == "every Tuesday"


# ── the brain-side guard ───────────────────────────────────────────────────

@pytest.mark.parametrize("fragment", ["every week.", "every Tuesday."])
def test_the_brain_asks_a_fragment_back_instead_of_answering_it(fragment):
    reply = api._try_voice_fragment_guard(fragment, [])
    assert reply and "part of a sentence" in reply
    # The failure to prevent is an encyclopedia answer; nothing like one may
    # appear.
    assert "recurring day of the week" not in reply


@pytest.mark.parametrize("command", ["stop", "next", "louder", "yes",
                                     "what time is it"])
def test_the_brain_guard_leaves_real_short_commands_alone(command):
    assert api._try_voice_fragment_guard(command, []) is None


def test_a_fragment_answering_the_assistants_own_question_is_left_alone():
    history = [{"role": "assistant",
                "content": "Which days for those two? Name one per recipe."}]
    assert api._try_voice_fragment_guard("Wednesday.", history) is None


def test_the_fragment_guard_runs_inside_the_instant_answer_layer():
    assert "part of a sentence" in (api._try_instant_answer(
        "every Tuesday.", conversation_history=[]) or "")


# ══════════════════════════════════════════════════════════════════════════
# DEFECT B · the capture-side extension
# ══════════════════════════════════════════════════════════════════════════

def _voice_service():
    pytest.importorskip("numpy")
    pytest.importorskip("sounddevice")
    from puffpai.voice import service
    return service


class FakeSTT:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0

    def transcribe(self, pcm):
        self.calls += 1
        return self.texts.pop(0) if self.texts else ""


def test_a_fragment_reopens_the_mic_exactly_once(monkeypatch):
    service = _voice_service()
    calls = []

    def _record(stream, first, wait_override=0.0):
        calls.append(wait_override)
        return b"\x00\x01"

    monkeypatch.setattr(service, "record_utterance", _record)
    monkeypatch.setattr(service, "normalize_utterance", lambda pcm: pcm)
    monkeypatch.setattr(service, "FRAGMENT_EXTEND", 1.5)
    stt = FakeSTT(["and Thursday I take the kids to soccer practice"])
    out = service.extend_if_fragment(object(), stt, "every Tuesday.")
    assert out == "every Tuesday and Thursday I take the kids to soccer practice"
    assert calls == [1.5], "the extension window must be bounded and single"


def test_silence_after_a_fragment_leaves_the_words_unchanged(monkeypatch):
    service = _voice_service()
    monkeypatch.setattr(service, "record_utterance",
                        lambda stream, first, wait_override=0.0: None)
    monkeypatch.setattr(service, "FRAGMENT_EXTEND", 1.5)
    assert service.extend_if_fragment(object(), FakeSTT([]),
                                      "every Tuesday.") == "every Tuesday."


def test_a_whole_sentence_never_reopens_the_mic(monkeypatch):
    service = _voice_service()
    def _boom(*a, **k):
        raise AssertionError("a complete utterance must not be extended")
    monkeypatch.setattr(service, "record_utterance", _boom)
    monkeypatch.setattr(service, "FRAGMENT_EXTEND", 1.5)
    assert service.extend_if_fragment(
        object(), FakeSTT([]), "set a timer for ten minutes"
    ) == "set a timer for ten minutes"


def test_the_guard_can_be_switched_off_by_env(monkeypatch):
    service = _voice_service()
    def _boom(*a, **k):
        raise AssertionError("PUFF_FRAGMENT_EXTEND=0 must disable the guard")
    monkeypatch.setattr(service, "record_utterance", _boom)
    monkeypatch.setattr(service, "FRAGMENT_EXTEND", 0.0)
    assert service.extend_if_fragment(object(), FakeSTT([]),
                                      "every Tuesday.") == "every Tuesday."


def test_the_silence_hang_default_is_unchanged():
    """The guard is a conditional extension, NOT a global latency tax."""
    service = _voice_service()
    assert service.SILENCE_HANG == 1.0


# ── helper ─────────────────────────────────────────────────────────────────

def _committing(app):
    real = mw.commit

    def _commit(targets, meal, days, app_=None):
        return real(targets, meal, days, app=app)

    return _commit


# ── a pending meal request must not swallow an unrelated turn ──────────────

@pytest.mark.parametrize("phrase", [
    "remind me Wednesday to call the vet",
    "set an alarm for Thursday at 6am",
    "put a dentist appointment on Thursday",
    "what's the weather on Wednesday",
])
def test_a_pending_request_does_not_claim_an_unrelated_day_turn(phrase, app,
                                                                monkeypatch):
    monkeypatch.setattr(mw, "commit", _committing(app))
    mw.set_pending(mw.extract_recipes(HISTORY), "dinner", [])
    assert mw.handle(phrase, HISTORY) is None
    assert app.plan == []
    assert mw.get_pending() is not None, "the request stays pending"


def test_a_bare_day_answer_still_completes_the_pending_request(app,
                                                               monkeypatch):
    monkeypatch.setattr(mw, "commit", _committing(app))
    mw.set_pending(mw.extract_recipes(HISTORY)[:1], "dinner", [])
    mw.handle("Wednesday", HISTORY)
    assert [(e["day"], e["meal"]) for e in app.plan] == [("wednesday", "dinner")]


# ══════════════════════════════════════════════════════════════════════════
# The week planner must never eat a single-dish request, and must
# never replace a real plan without being asked
#
# "can you put Tacos on the meal plan for Thursday dinner" matches a loose
# _MEAL_PLAN_RE ("plan... dinner" inside a 24-character window). Sent to
# auto_meal_plan, it would rewrite all seven days of the household's real
# mealPlan and regenerate the whole shoppingList, the named dish would never
# appear, and the reply would say only "Dinner plan saved to FreshTable".
# Recovery would need a generational backup.
# ══════════════════════════════════════════════════════════════════════════

from puffpai.agents import meal_plan_guard as guard  # noqa: E402


class FakePlanApp:
    """A FreshTable whose mealPlan the guard can count."""

    def __init__(self, days=()):
        self.plan = [{"day": d, "meal": "dinner", "dish": "Dish " + d}
                     for d in days]

    def _load(self, app_id):
        return {"mealPlan": list(self.plan), "recipes": [], "shoppingList": []}

    def data_path(self, app_id):
        return "/tmp/" + app_id + ".json"


def _plan_tools(fired, reply="Dinner plan saved to FreshTable:\n  - Monday: Chili"):
    def _invoke(_arg):
        fired.append(1)
        return reply

    class _Tools:
        tool_dict = {"auto_meal_plan": type("T", (), {"invoke": staticmethod(_invoke)})()}
    return _Tools()


@pytest.fixture(autouse=True)
def _clean_plan_pending():
    guard.clear_pending()
    yield
    guard.clear_pending()


#: Every one of these names ONE dish, ONE day or one slot. None may reach
#: auto_meal_plan. The fourth is the tacos sentence described above.
SINGLE_ITEM_PHRASES = [
    "put that on Thursday's meal plan",
    "put chicken stir fry on the meal plan for Thursday dinner",
    "add that to the meal plan on Wednesday",
    "can you put SAMPLE-Tacos on the meal plan for Thursday dinner",
    "add that salmon recipe to Wednesday's dinner this week",
    "put that chicken stir fry on Thursday's dinner plan",
    "schedule tacos for dinner on Friday",
    "stick the salmon on Monday's meal plan",
    "set thursday dinner to SAMPLE-Tacos",
    "make that Wednesday's dinner",
]


@pytest.mark.parametrize("phrase", SINGLE_ITEM_PHRASES)
def test_a_single_dish_phrase_is_recognised_as_a_single_item(phrase):
    assert guard.names_a_single_item(phrase) is True


@pytest.mark.parametrize("phrase", SINGLE_ITEM_PHRASES)
def test_a_single_dish_phrase_never_reaches_auto_meal_plan(phrase, monkeypatch):
    """The core rule: none of these may fire the destructive week planner."""
    fired = []
    monkeypatch.setattr(api, "_app_installed", lambda app_id: True)
    monkeypatch.setattr(api, "_app_unavailable_reason", lambda app_id: None)
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _plan_tools(fired))
    monkeypatch.setattr(guard, "_app", lambda: FakePlanApp(["monday"]))

    # A phrase whose dish/day cannot be resolved ASKS rather than going quiet
    # (an unanswered turn reaches the agent route, which can still call
    # auto_meal_plan). Either way the week planner must not fire.
    reply = api._try_meal_plan(phrase)
    assert reply is None or reply == guard.SINGLE_ITEM_ASK, reply
    assert fired == [], "auto_meal_plan fired for " + repr(phrase)


WEEK_PHRASES = [
    "plan this week's meals",
    "make me a meal plan for the week",
    "plan meals for the week",
    "sort out the dinners for the week",
]


@pytest.mark.parametrize("phrase", WEEK_PHRASES)
def test_the_real_week_planner_phrasings_are_not_single_items(phrase):
    assert guard.names_a_single_item(phrase) is False


@pytest.mark.parametrize("phrase", WEEK_PHRASES[:2])
def test_a_week_plan_over_an_existing_plan_asks_first(phrase, monkeypatch):
    fired = []
    app5 = FakePlanApp(["monday", "tuesday", "wednesday", "thursday", "friday"])
    monkeypatch.setattr(api, "_app_installed", lambda app_id: True)
    monkeypatch.setattr(api, "_app_unavailable_reason", lambda app_id: None)
    monkeypatch.setattr(guard, "_app", lambda: app5)
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _plan_tools(fired))

    reply = api._try_meal_plan(phrase)
    assert reply == ("This will replace your current 5-day plan and rebuild "
                     "the shopping list. Say 'yes, replace the plan' to "
                     "continue.")
    assert fired == [], "nothing may be written before the confirmation"


def test_yes_replace_the_plan_then_writes_and_names_what_it_replaced(
        monkeypatch):
    fired = []
    app5 = FakePlanApp(["monday", "tuesday", "wednesday", "thursday", "friday"])
    monkeypatch.setattr(api, "_app_installed", lambda app_id: True)
    monkeypatch.setattr(api, "_app_unavailable_reason", lambda app_id: None)
    monkeypatch.setattr(guard, "_app", lambda: app5)
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _plan_tools(fired))

    assert api._try_meal_plan("plan this week's meals").startswith("This will")
    reply = api._try_meal_plan("yes, replace the plan")
    assert fired == [1]
    assert "Replaced a 5-day plan" in reply
    assert "backup slot 1" in reply
    assert "Monday: Chili" in reply
    assert guard.get_pending() is None


def test_declining_the_confirmation_writes_nothing(monkeypatch):
    fired = []
    monkeypatch.setattr(api, "_app_installed", lambda app_id: True)
    monkeypatch.setattr(api, "_app_unavailable_reason", lambda app_id: None)
    monkeypatch.setattr(guard, "_app", lambda: FakePlanApp(["monday", "tuesday"]))
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _plan_tools(fired))

    api._try_meal_plan("plan this week's meals")
    reply = api._try_meal_plan("no, leave it alone")
    assert fired == []
    assert "left your meal plan" in reply
    assert guard.get_pending() is None


def test_an_empty_plan_proceeds_but_still_says_what_it_did(monkeypatch):
    fired = []
    monkeypatch.setattr(api, "_app_installed", lambda app_id: True)
    monkeypatch.setattr(api, "_app_unavailable_reason", lambda app_id: None)
    monkeypatch.setattr(guard, "_app", lambda: FakePlanApp([]))
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _plan_tools(fired))

    reply = api._try_meal_plan("plan this week's meals")
    assert fired == [1]
    assert "Monday: Chili" in reply
    assert "nothing was replaced" in reply
    assert "shopping list was rebuilt" in reply


def test_a_stale_confirmation_cannot_be_committed_later(monkeypatch):
    fired = []
    monkeypatch.setattr(api, "_app_installed", lambda app_id: True)
    monkeypatch.setattr(api, "_app_unavailable_reason", lambda app_id: None)
    monkeypatch.setattr(guard, "_app", lambda: FakePlanApp(["monday"]))
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _plan_tools(fired))

    api._try_meal_plan("plan this week's meals")
    monkeypatch.setattr(guard, "PENDING_TTL", -1)
    assert api._try_meal_plan("yes, replace the plan") is None
    assert fired == []


def test_the_week_planner_takes_a_generational_backup_before_replacing():
    """The backup lives in auto_meal_plan itself, so the agent route gets it."""
    src = Path(__file__).resolve().parents[1] / "src" / "puffpai" / "assistant_tools.py"
    body = src.read_text(encoding="utf-8")
    head = body.index("def auto_meal_plan(")
    chunk = body[head:body.index("def meal_set(", head)]
    assert "rotate_backup" in chunk
    assert chunk.index("rotate_backup") < chunk.index("fresh_set_meal")


# ══════════════════════════════════════════════════════════════════════════
# A dish said out loud reaches meal_set, and the fallback never
# suggests the sentence the user just said
# ══════════════════════════════════════════════════════════════════════════

DIRECT_PHRASES = [
    ("Set thursday dinner to SAMPLE-Tacos", "thursday", "dinner",
     "SAMPLE-Tacos"),
    ("set wednesday's lunch to chicken salad", "wednesday", "lunch",
     "chicken salad"),
    ("thursday dinner is beef stew", "thursday", "dinner", "beef stew"),
    ("Friday's dinner will be pizza", "friday", "dinner", "pizza"),
    ("make lasagna Tuesday's dinner", "tuesday", "dinner", "lasagna"),
    ("tacos for dinner on Thursday", "thursday", "dinner", "tacos"),
    ("set dinner on Monday to pot roast", "monday", "dinner", "pot roast"),
    ("can you put SAMPLE-Tacos on the meal plan for Thursday dinner",
     "thursday", "dinner", "SAMPLE-Tacos"),
    ("put chicken stir fry on Thursday's dinner", "thursday", "dinner",
     "chicken stir fry"),
]


@pytest.mark.parametrize("phrase,day,meal,dish", DIRECT_PHRASES)
def test_a_dish_said_out_loud_is_parsed_into_a_slot(phrase, day, meal, dish):
    assert mw.parse_direct_set(phrase) == (day, meal, dish)


def test_a_direct_set_sentence_writes_the_slot_instead_of_going_to_rag(app):
    reply = mw.handle_direct("Set thursday dinner to SAMPLE-Tacos", app=app)
    assert app.plan == [{"day": "thursday", "meal": "dinner",
                         "dish": "SAMPLE-Tacos", "notes": None,
                         "recipeId": None}]
    assert reply == "Thursday dinner is set to SAMPLE-Tacos."


def test_the_direct_set_is_reached_through_the_meal_add_hook(monkeypatch):
    monkeypatch.setattr(api, "_app_installed", lambda app_id: True)
    seen = {}

    def _fake(msg, app=None):
        seen["msg"] = msg
        return "written"

    monkeypatch.setattr(mw, "handle_direct", _fake)
    assert api._try_meal_add("Set thursday dinner to SAMPLE-Tacos", []) == "written"
    assert seen["msg"] == "Set thursday dinner to SAMPLE-Tacos"


@pytest.mark.parametrize("phrase", [
    "what's for dinner on Thursday",
    "what is thursday's dinner",
    "show me the meal plan",
    "plan this week's meals",
    "make me a meal plan for the week",
    "put that on Thursday's meal plan",
    "add that to the meal plan on Wednesday",
    "remind me Wednesday to call the vet",
])
def test_the_direct_set_declines_reads_pronouns_and_the_week_planner(phrase):
    assert mw.parse_direct_set(phrase) is None


def test_a_context_recipe_still_wins_over_the_direct_set(app, monkeypatch):
    """A phrasing that points at a recipe in the conversation keeps writing
    the real recipe, not a bare dish name."""
    monkeypatch.setattr(mw, "commit", _committing(app))
    reply = mw.handle("add that salmon recipe to Wednesday's dinner this week",
                      HISTORY)
    assert "Wednesday" in reply
    assert app.plan and app.plan[0]["recipeId"], "the slot must link a recipe"


def test_the_fallback_never_suggests_the_users_own_sentence():
    said = "Set thursday dinner to SAMPLE-Tacos"
    echoed = ("I haven't done that. Please try saying 'set thursday dinner to "
              "SAMPLE-Tacos' as a direct command.")
    fixed = api._strip_echoed_suggestion(said, echoed)
    assert said.lower() not in fixed.lower()
    assert "meal-plan request" in fixed
    assert "Thursday dinner to tacos" in fixed


def test_a_different_suggestion_is_left_exactly_as_it_was():
    said = "can you turn the kitchen light on for me"
    reply = ("I haven't done that. Try saying 'turn on the kitchen lights' as "
             "a direct command.")
    assert api._strip_echoed_suggestion(said, reply) == reply


def test_the_echo_test_ignores_case_and_punctuation():
    said = "  Set Thursday Dinner To Tacos!!  "
    reply = "Try saying 'set thursday dinner to tacos' as a direct command."
    fixed = api._strip_echoed_suggestion(said, reply)
    assert fixed != reply
    assert "couldn't work out" in fixed


# ══════════════════════════════════════════════════════════════════════════
# Four properties of the real request path
# ══════════════════════════════════════════════════════════════════════════
#
# 1. HISTORY SHAPE. Every test above builds history as [{"role", "content"}].
# No production caller does: all four chat entry points hand on the PAIRED
# [{"user", "assistant"}] shape from _convert_openai_messages_to_history.
# A fastpath that read only msg["role"] would see no conversation at all on a
# real request, and the whole conversation-aware write would be a no-op.
# These tests go through the REAL Flask endpoint, so the shape is whatever
# production actually produces.

PAIRED_HISTORY = [
    {"user": "Can I get a salmon recipe?", "assistant": SALMON_1},
    {"user": "Have you got another one?", "assistant": SALMON_2},
]


def test_the_paired_production_history_shape_yields_its_recipes():
    found = mw.extract_recipes(PAIRED_HISTORY)
    assert len(found) == 2, "the shape every production caller passes"
    assert "Pan-Seared Salmon" in found[0]["title"]
    assert "Herb-Crusted Salmon" in found[1]["title"]


def test_both_history_shapes_find_the_same_recipes():
    assert ([r["title"] for r in mw.extract_recipes(PAIRED_HISTORY)]
            == [r["title"] for r in mw.extract_recipes(HISTORY)])


def test_the_fragment_guard_stands_down_on_the_paired_shape():
    """The stand-down depends on reading the paired shape too."""
    history = [{"user": "add both of those",
                "assistant": "Which days for those two? Name one per recipe."}]
    assert api._try_voice_fragment_guard("Wednesday.", history) is None


@pytest.fixture
def chat_client(monkeypatch, app):
    """The real /api/chat endpoint, with FreshTable faked and no model."""
    import puffpai.app_data as _app_data
    monkeypatch.setattr(_app_data, "AppData", lambda *a, **k: app)
    monkeypatch.setattr(api, "_app_installed", lambda app_id: True)
    monkeypatch.setattr(api, "_app_unavailable_reason", lambda app_id: None)
    # Nothing in these turns may need a model; if one does, the assertions
    # below fail loudly rather than the suite reaching out to Ollama.
    monkeypatch.setattr(api, "_try_team_chat",
                        lambda *a, **k: "[no model in tests]")
    monkeypatch.setattr(api, "_query_ollama_direct",
                        lambda *a, **k: "[no model in tests]")
    # api._get_app_data caches its result in the module-level
    # _app_data_instance global, outside monkeypatch's reach: once a test
    # calls it, every later test (even with AppData restored) gets this
    # fake `app` back. api._chat_history is the same story — every real
    # /api/chat call appends the fake turn to that module-level list, and it
    # is never scoped to the test, so a later test's read of "what got
    # persisted" sees THIS test's turn. api._voice_full_tl (a threading.local)
    # is a third: _process_voice_query stashes the full untruncated reply
    # there for the transcript recorder, and it is only ever overwritten by a
    # REAL call to _process_voice_query — a later test that stubs that
    # function out (as another suite does) never touches it, so
    # its transcript recorder reads back THIS test's stale full text. Save
    # /restore all three by hand, plus the TESTING flag.
    had_testing = 'TESTING' in api.app.config
    prev_testing = api.app.config.get('TESTING')
    prev_app_data_instance = getattr(api, "_app_data_instance", None)
    prev_chat_history = list(api._chat_history)
    prev_voice_full_text = getattr(api._voice_full_tl, "text", None)
    api.app.config['TESTING'] = True
    try:
        with api.app.test_client() as c:
            yield c
    finally:
        api._app_data_instance = prev_app_data_instance
        api._chat_history[:] = prev_chat_history
        api._voice_full_tl.text = prev_voice_full_text
        if had_testing:
            api.app.config['TESTING'] = prev_testing
        else:
            api.app.config.pop('TESTING', None)


def _say(client, wire, text):
    """One spoken turn on the wire, exactly as a client sends it."""
    wire.append({"role": "user", "content": text})
    resp = client.post('/api/chat', json={"messages": wire})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    reply = resp.get_json()['message']['content']
    wire.append({"role": "assistant", "content": reply})
    return reply


def test_the_salmon_conversation_end_to_end(chat_client, app):
    """The turns of the salmon conversation, through the real endpoint.

    This is the test that catches a history-shape mismatch: the unit tests
    above can all pass while production fails, because they feed a shape
    production never sends.
    """
    wire = [
        {"role": "user", "content": "Can I get a salmon recipe?"},
        {"role": "assistant", "content": SALMON_1},
        {"role": "user", "content": "Have you got another one?"},
        {"role": "assistant", "content": SALMON_2},
    ]
    asked = _say(chat_client, wire, ADD_BOTH)
    assert "day" in asked.lower(), asked
    assert "[no model in tests]" not in asked

    _say(chat_client, wire, NAME_DAYS)
    _say(chat_client, wire, CONFIRM)

    slots = {e["day"]: e for e in app.plan}
    assert set(slots) == {"wednesday", "thursday"}, app.plan
    assert "Pan-Seared" in slots["wednesday"]["dish"]
    assert "Herb-Crusted" in slots["thursday"]["dish"]
    assert slots["wednesday"]["recipeId"], "the slot must LINK the recipe"
    assert slots["thursday"]["recipeId"], "the slot must LINK the recipe"


def test_add_that_salmon_recipe_links_the_recipe_not_the_word(chat_client, app):
    """Degrading to handle_direct would write dish='salmon' with no recipeId."""
    wire = [
        {"role": "user", "content": "Can I get a salmon recipe?"},
        {"role": "assistant", "content": SALMON_1},
    ]
    _say(chat_client, wire, "add that salmon recipe to Wednesday's dinner")
    assert len(app.plan) == 1, app.plan
    slot = app.plan[0]
    assert slot["day"] == "wednesday" and slot["meal"] == "dinner"
    assert slot["dish"].lower() != "salmon", "the word, not the recipe"
    assert "Pan-Seared" in slot["dish"]
    assert slot["recipeId"], "the slot must LINK the recipe"


# 2. ECHO GUARD. Replacing the WHOLE reply whenever any quoted span equals the
# user's message is wrong, because the model quotes the question back as a
# matter of habit, so real answers would become timer boilerplate.

def test_an_answer_that_quotes_the_question_keeps_its_answer():
    said = "tell me about salmon"
    reply = ('You asked "tell me about salmon". Salmon are anadromous fish '
             'that hatch in fresh water, migrate to the ocean, and return to '
             'spawn. Most of what is sold is farmed Atlantic salmon.')
    assert api._strip_echoed_suggestion(said, reply) == reply


def test_a_short_quoted_question_still_keeps_its_answer():
    said = "what is a mirepoix"
    reply = ('"what is a mirepoix" — it is diced onion, carrot and celery, '
             'the base of most French braises.')
    assert api._strip_echoed_suggestion(said, reply) == reply


def test_a_long_answer_keeps_everything_except_the_useless_suggestion():
    said = "how do I purify water"
    reply = ("Boiling is the most reliable method available in a home: bring "
             "the water to a rolling boil for one full minute, or three "
             "minutes above 2,000 metres. Chemical treatment with unscented "
             "household bleach works when fuel is short, at two drops per "
             "litre, left to stand for thirty minutes. Filtration removes "
             "sediment and most protozoa but not viruses, so it is usually "
             "paired with one of the other two. "
             "Try saying \"how do I purify water\" as a direct command.")
    fixed = api._strip_echoed_suggestion(said, reply)
    assert "rolling boil" in fixed and "two drops per litre" in fixed
    assert "as a direct command" not in fixed


def test_the_knowledge_search_topic_path_never_strips(monkeypatch):
    """_try_knowledge_search passes the TOPIC as user_message, so an answer
    that quotes its own topic must not be mistaken for an echoed suggestion."""
    captured = {}

    def _fake(user_message, history, persona, enriched=False, strip_echo=True):
        captured['strip_echo'] = strip_echo
        return {'response': 'Water purification is …', 'sources': [],
                'suggestions': []}

    from puffpai.agents import knowledge_reads as _kr
    monkeypatch.setattr(_kr, "match", lambda text: "water purification")
    monkeypatch.setattr(api, "_get_rag_context",
                        lambda topic: {'hits': ['h'], 'sources': []})
    monkeypatch.setattr(api, "_query_ollama_direct", _fake)
    out = api._try_knowledge_search("search my library for water purification")
    assert out['response'] == 'Water purification is …'
    assert captured['strip_echo'] is False


def test_the_boilerplate_grammar_is_fixed():
    said = "do the thing with the stuff"
    reply = "Try saying 'do the thing with the stuff' as a direct command."
    fixed = api._strip_echoed_suggestion(said, reply)
    assert "an action request" in fixed
    assert "a action request" not in fixed


# 3. _DECLINE_RE HIJACK. For 600 s after a confirmation prompt, a loose decline
# pattern would let any sentence starting with stop/cancel/don't answer the
# meal plan instead, throwing the real command away.

@pytest.fixture
def _pending_plan(monkeypatch):
    fired = []
    monkeypatch.setattr(api, "_app_installed", lambda app_id: True)
    monkeypatch.setattr(api, "_app_unavailable_reason", lambda app_id: None)
    monkeypatch.setattr(guard, "_app",
                        lambda: FakePlanApp(["monday", "tuesday"]))
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _plan_tools(fired))
    assert api._try_meal_plan("plan this week's meals").startswith("This will")
    assert guard.get_pending() is not None
    yield fired
    guard.clear_pending()


@pytest.mark.parametrize("command", [
    "stop the music",
    "cancel the timer",
    "do not turn on the lights",
    "turn off the bedroom lamp",
    "cancel my 3pm meeting",
])
def test_a_real_command_is_not_eaten_by_the_pending_confirmation(
        command, _pending_plan):
    assert api._try_meal_plan(command) is None, command
    assert _pending_plan == [], "and nothing was written either"
    assert guard.get_pending() is not None, "the question is still standing"


@pytest.mark.parametrize("answer", ["no", "nope", "never mind",
                                    "no, leave it alone"])
def test_a_bare_decline_still_declines(answer, _pending_plan):
    reply = api._try_meal_plan(answer)
    assert "left your meal plan" in (reply or "")
    assert _pending_plan == []
    assert guard.get_pending() is None


def test_yes_replace_the_plan_still_confirms(_pending_plan):
    reply = api._try_meal_plan("yes, replace the plan")
    assert _pending_plan == [1]
    assert "Replaced a 2-day plan" in reply


# 4. SILENT None DECLINES. An unresolved slot request that returned None would
# reach the agent route, which can still call auto_meal_plan.

@pytest.mark.parametrize("phrase", [
    "put that on Thursday's meal plan",
    "add that to the meal plan on Wednesday",
    "plan tacos for Thursday",
    "meal plan: Thursday tacos",
    "update the meal plan, Thursday is tacos",
])
def test_an_unresolvable_slot_request_asks_instead_of_going_quiet(
        phrase, monkeypatch):
    fired = []
    monkeypatch.setattr(api, "_app_installed", lambda app_id: True)
    monkeypatch.setattr(api, "_app_unavailable_reason", lambda app_id: None)
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _plan_tools(fired))
    monkeypatch.setattr(guard, "_app", lambda: FakePlanApp(["monday"]))
    reply = api._try_meal_plan(phrase)
    assert reply == guard.SINGLE_ITEM_ASK, phrase
    assert fired == [], "and the week planner still never fires"


@pytest.mark.parametrize("phrase", [
    "plan the week starting Monday",
    "plan meals for this week starting Monday",
])
def test_a_start_anchor_is_a_week_not_a_slot(phrase, monkeypatch):
    """A weekday after "starting" anchors a WEEK — it must reach the
    confirmation path, not be declined as a single-item turn."""
    assert guard.names_a_single_item(phrase) is False
    fired = []
    app3 = FakePlanApp(["monday", "tuesday", "wednesday"])
    monkeypatch.setattr(api, "_app_installed", lambda app_id: True)
    monkeypatch.setattr(api, "_app_unavailable_reason", lambda app_id: None)
    monkeypatch.setattr(guard, "_app", lambda: app3)
    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _plan_tools(fired))
    reply = api._try_meal_plan(phrase)
    assert reply == ("This will replace your current 3-day plan and rebuild "
                     "the shopping list. Say 'yes, replace the plan' to "
                     "continue.")
    assert fired == []
    guard.clear_pending()


def test_auto_meal_plan_itself_refuses_an_unconfirmed_replace(monkeypatch):
    """The agent route can call the tool directly — so the refusal lives in
    the TOOL, not only in the guarded fastpath."""
    from puffpai import assistant_tools as rt
    # AssistantTools binds AppData with a `from app_data import AppData` inside
    # create_tools, so the fake has to be in place BEFORE construction.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"
                           / "puffpai"))
    import app_data as _flat

    class _Store:
        def __init__(self):
            self.written = []

        def _load(self, app_id):
            return {"mealPlan": [{"day": "monday", "dish": "Chili"},
                                 {"day": "tuesday", "dish": "Soup"}],
                    "shoppingList": ["milk"], "recipes": []}

        def data_path(self, app_id):
            return "unused"

        def fresh_set_meal(self, *a, **k):
            self.written.append(a)

        def fresh_grocery_from_plan(self):
            self.written.append("grocery")

    store = _Store()
    monkeypatch.setattr(_flat, "AppData", lambda *a, **k: store)
    fn = rt.AssistantTools().tool_dict['auto_meal_plan']
    reply = fn.invoke({})
    assert "replace your current 2-day plan" in reply
    assert store.written == [], "nothing may be written without confirmation"
    guard.clear_pending()
