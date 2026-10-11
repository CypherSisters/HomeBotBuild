"""the ORDER of the hooks inside ``_try_instant_answer``.

Six branches (meals-voice, brain-2, calendar-nl, media-2, house-2, compound-2)
each slotted new fastpaths into the same dispatcher. Git merge order is NOT the
spec -- the spec is deterministic, most-specific-first, safety before
convenience. Every rule below
is one that was observed to MISBEHAVE when the two calls were the other way
round, so this file pins the relative positions rather than the absolute ones
(new hooks may be added between any pair without touching this test).

The check is structural: parse the dispatcher's own source and read the order of
the ``_try_*`` calls. A test that only exercised behaviour would pass on a
wrongly-ordered chain whenever the earlier matcher happened to decline.

Two documented, deliberate deviations from the plan's arrow order:

* ``_try_weather_alerts`` runs BEFORE ``_try_weather_answer``, not after. The
  plan text reads "weather -> weather alerts" but its own stated principle is
  most-specific-first, and "any severe weather alerts" contains the word
  "weather": with the forecast fastpath first the alerts question is answered
  with a plain forecast. Specific before generic wins.
* The compound splitter is not a call inside this dispatcher at all. It is an
  outer layer (``_try_compound_request`` in ``_process_chat_query``); inside
  this function the compound turn STANDS DOWN at the top via
  ``_is_compound_request``, and each clause re-enters the whole chain. That is
  stronger than the plan's position 8 -- the fragment guard and chit-chat can
  never see a whole compound turn -- so it is pinned here in that form.
"""

import ast
import inspect
import re

import pytest

from puffpai import api_server


DISPATCHER = "_try_instant_answer"


def _call_order():
    """[(lineno, name)] for every ``_try_*`` call in the dispatcher, in order."""
    src = inspect.getsource(getattr(api_server, DISPATCHER))
    tree = ast.parse(re.sub(r"^ +", "", src, count=1) if src.startswith(" ") else src)
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef) and fn.name == DISPATCHER
    seen = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None)
        if name and name.startswith("_try_"):
            seen.append((node.lineno, name))
    seen.sort()
    return seen


@pytest.fixture(scope="module")
def positions():
    """name -> index of its FIRST call in the chain."""
    out = {}
    for index, (_lineno, name) in enumerate(_call_order()):
        out.setdefault(name, index)
    return out


def _before(positions, earlier, later, why):
    for name in (earlier, later):
        assert name in positions, (
            f"{name} is no longer called from {DISPATCHER}; the precedence "
            f"rule it belonged to ({why}) needs re-deciding, not deleting."
        )
    assert positions[earlier] < positions[later], (
        f"{earlier} must run BEFORE {later}: {why}. "
        f"Actual order: {earlier}=#{positions[earlier]}, "
        f"{later}=#{positions[later]}."
    )


# --- 1. safety before convenience ------------------------------------------

def test_untargeted_house_gate_precedes_media_and_device_matchers(positions):
    """media-2 extended the whole-house gate; a refusal must outrank an action.

    "turn off all the lights in the house" reaching a device matcher first is
    how an untargeted command becomes a real fan-out of HA calls.
    """
    for later in ("_try_media_play", "_try_media_stop", "_try_media_volume"):
        _before(positions, "_try_untargeted_house_command", later,
                "an untargeted whole-house command must be refused before any "
                "matcher can act on part of it")


# --- 3. clock -> alerts -> weather -> timers/alarms -------------------------

def test_weather_alerts_precede_the_forecast_fastpath(positions):
    _before(positions, "_try_weather_alerts", "_try_weather_answer",
            "'any severe weather alerts' contains 'weather' and is swallowed "
            "by the forecast fastpath if that runs first (most-specific-first)")


def test_clock_precedes_weather(positions):
    _before(positions, "_try_time_answer", "_try_weather_alerts",
            "the clock fastpath keeps its place at the head of the chain")


def test_alarm_clarify_precedes_alarm_set(positions):
    _before(positions, "_try_alarm_clarify", "_try_alarm_set",
            "an ambiguous alarm must be clarified, not silently set")


# --- 4. calendar delete -> calendar recurring -> add task -------------------

def test_calendar_delete_precedes_recurring_and_add_task(positions):
    """Both orderings are failure modes if reversed.

    delete-after-add_task: "delete the soccer reminder" would be stored as a
    NEW task. delete-before-alarms: "delete the alarm" would stop reaching
    AlarmFlow.
    """
    _before(positions, "_try_calendar_delete", "_try_calendar_recurring",
            "a delete is more specific than a recurrence create")
    _before(positions, "_try_calendar_delete", "_try_add_task",
            "_try_add_task only ADDS, so a delete reaching it first is stored "
            "as a new task and the planner confirms a deletion that never ran")
    _before(positions, "_try_calendar_recurring", "_try_add_task",
            "'every weekday at 6:15 take the kids to practice' is a repeating "
            "calendar item; _try_add_task stored the whole sentence as one task")


def test_the_medication_dose_rule_sits_between_the_calendar_and_task_hooks(
        positions):
    """"set up a daily reminder to take my <med>" must not become
    a plain to-do or agent guesswork; "I took my <med> this
    morning" must match too. The dose rule runs ahead of _try_add_task
    so a medication ON FILE is logged and scheduled as a medication, and it
    declines an unknown name so the reminder phrasing still
    reaches _try_add_task untouched."""
    _before(positions, "_try_calendar_recurring", "_try_med_log",
            "a recurring calendar item is still a calendar item")
    _before(positions, "_try_med_log", "_try_add_task",
            "a medication dose is not a to-do, and the reply must not read "
            "the drug name out loud")


def test_the_vault_open_line_precedes_the_show_and_read_paths(positions):
    """"open the vault" must not spend time on the agent
    graph to say the one thing that is always true. It must also stay ahead of
    _try_show, which owns "show me X" and would open a viewer instead."""
    _before(positions, "_try_vault_open", "_try_show",
            "'show me the vault' is a PIN question, not a display request")
    _before(positions, "_try_vault_open", "_try_capability_read",
            "the deterministic sentence beats any capability dispatch")


def test_calendar_delete_runs_after_the_alarm_fastpaths(positions):
    for earlier in ("_try_timer_set", "_try_alarm_set", "_try_alarm_clarify"):
        _before(positions, earlier, "_try_calendar_delete",
                "'delete the alarm' must still reach AlarmFlow")


# --- 5. meal add / direct set -> meal plan ---------------------------------

def test_shopping_add_sits_between_the_task_and_the_meal_fastpaths(positions):
    """A one-item grocery WRITE has its own hook.

    Both neighbours were observed to misread the same sentence: _try_meal_plan
    turns "add SAMPLE oat milk to the shopping list" into a whole-week
    auto_meal_plan run, and _try_capability_read answers a WRITE with the list
    it was supposed to change. _try_add_task keeps its place above it — it
    already excludes shopping/grocery, and "remind me to buy oat milk" is a
    reminder.
    """
    _before(positions, "_try_add_task", "_try_shopping_add",
            "'remind me to buy milk' is a reminder; _try_add_task already "
            "excludes the shopping/grocery phrasings this hook claims")
    _before(positions, "_try_shopping_add", "_try_meal_plan",
            "a one-item grocery add must not be escalated into a full "
            "auto_meal_plan run that replaces the week")
    _before(positions, "_try_shopping_add", "_try_capability_read",
            "a write must not be answered by the read of the list it was "
            "supposed to change")


def test_meal_add_precedes_meal_plan(positions):
    """"add that to Wednesday's dinner" is a single-day write. Reaching
    _try_meal_plan first fires auto_meal_plan and REPLACES the whole week.
    """
    _before(positions, "_try_meal_add", "_try_meal_plan",
            "a single-day meal write must not be escalated into a full "
            "auto_meal_plan run that replaces the week")


# --- 6. now-playing -> tunein -> media play/stop ---------------------------

def test_now_playing_precedes_tunein_and_playback(positions):
    _before(positions, "_try_now_playing", "_try_tunein_radio",
            "'what's playing' is a read and must not start a stream")
    for later in ("_try_media_play", "_try_media_stop"):
        _before(positions, "_try_now_playing", later,
                "'what's playing' is a read, not a transport command")


def test_tunein_precedes_generic_media_play(positions):
    _before(positions, "_try_tunein_radio", "_try_media_play",
            "a named call sign belongs to the egress-gated tuner before the "
            "generic local-catalog play matcher can claim it")


# --- 7. capability read -> household reads ---------------------------------

def test_code_request_sits_between_the_household_writes_and_the_reads(positions):
    """An explicit ask to WRITE CODE has its own hook.

    Both neighbours misread the same sentences. Ahead of it, the household
    writes own their verbs: "add oat milk to the shopping list" and "create a
    reminder for 5pm" are a grocery add and a reminder, never a program.
    Behind it, the reads answer a coding ask with a capability blurb (the
    tables match on "function"/"automation") or, further down, with RAG prose
    invented from a library shelf.
    """
    for earlier in ("_try_add_task", "_try_shopping_add", "_try_meal_add",
                    "_try_untargeted_house_command"):
        _before(positions, earlier, "_try_code_request",
                "a household write owns its own verb; a coding hook must not "
                "claim 'add ... to the shopping list' or 'create a reminder'")
    for later in ("_try_capability_read", "_try_registry_read"):
        _before(positions, "_try_code_request", later,
                "a coding ask answered by a capability read is a blurb where "
                "the user asked for code")


def test_capability_read_precedes_household_read(positions):
    _before(positions, "_try_capability_read", "_try_household_read",
            "the capread table (pantry/bills, house-2) is the more specific "
            "reader and must outrank the generic household read")


# --- 9/10. the two LATE guards, and the loosest matcher LAST ---------------

LATE_GUARDS = ("_try_voice_fragment_guard", "_try_chitchat")

EVERY_REAL_COMMAND = (
    "_try_time_answer", "_try_weather_alerts", "_try_weather_answer",
    "_try_timer_set", "_try_alarm_set", "_try_alarm_clarify",
    "_try_calendar_delete", "_try_calendar_recurring", "_try_add_task",
    "_try_shopping_add", "_try_meal_add", "_try_meal_plan",
    "_try_untargeted_house_command", "_try_now_playing", "_try_tunein_radio",
    "_try_media_play", "_try_media_stop", "_try_media_volume",
    "_try_capability_read", "_try_household_read", "_try_alarm_dismiss",
    "_try_code_request",
)


@pytest.mark.parametrize("guard", LATE_GUARDS)
@pytest.mark.parametrize("command", EVERY_REAL_COMMAND)
def test_late_guards_run_after_every_real_command(positions, guard, command):
    """A short real command ("stop", "louder", "next") must be long gone.

    The fragment guard asks the sentence back and chit-chat answers socially;
    either one placed early eats a genuine command.
    """
    _before(positions, command, guard,
            "a real short command must have had its chance before a guard "
            "that answers instead of acting")


def test_fragment_guard_precedes_chitchat(positions):
    _before(positions, "_try_voice_fragment_guard", "_try_chitchat",
            "an obvious sentence fragment is asked back before it is treated "
            "as small talk")


def test_registry_read_is_the_last_of_the_appliances_own_layers(positions):
    """Pre-existing and deliberate: the declared-phrase layer is the loosest.

    Above _try_alarm_dismiss it claimed "silence my alarm" on the word "alarm"
    and returned the alarm LIST while the alarm kept ringing.

    It is no longer the terminal CALL -- ``_try_skill_dispatch`` sits below it
    (see the next test) -- but it is still the last thing the appliance's own
    declared capabilities get to do, which is what this rule was about.
    """
    order = [name for _lineno, name in _call_order()]
    assert order[-2:] == ["_try_registry_read", "_try_skill_dispatch"], (
        "_try_registry_read must remain the last of the appliance's own "
        "matchers, with only the skill dispatcher below it. Tail is "
        f"{order[-2:]}."
    )
    for guard in LATE_GUARDS:
        _before(positions, guard, "_try_registry_read",
                "the hand-tuned guards are decided before the loosest matcher")


def test_skill_dispatch_is_pinned_last(positions):
    """A skill answers only after EVERYTHING the appliance ships has declined.

    A skill is code the box did not ship, running against a
    door the household opened by hand, so it gets the last turn and the
    narrowest matcher (whole-utterance equality with a declared trigger).

    Two defences, and this is the second one: ``skills.manifest.
    trigger_collision`` refuses to INSTALL a skill whose trigger any capability
    claims, so "turn off the lights" is not available to a skill at all. This
    ordering is what holds if that ever regresses.
    """
    order = _call_order()
    assert order[-1][1] == "_try_skill_dispatch", (
        "_try_skill_dispatch must be the last call in the chain; a skill that "
        f"outranks a declared capability is a skill answering for the house. "
        f"Last call is {order[-1][1]}."
    )
    for name, index in positions.items():
        if name != "_try_skill_dispatch":
            assert index < positions["_try_skill_dispatch"], (
                f"{name} now runs AFTER the skill dispatcher")


def test_skill_management_is_decided_before_the_late_guards(positions):
    """Listing/disabling/removing a skill is an ordinary read about the
    appliance, so it must not be swallowed by chit-chat or the fragment guard."""
    for hook in ("_try_skills_list", "_try_skill_disable", "_try_skill_remove"):
        for guard in LATE_GUARDS:
            _before(positions, hook, guard,
                    "a real request about skills beats the late guards")


# --- 8. the compound turn stands down at the TOP ---------------------------

def test_compound_turn_stands_down_before_every_fastpath():
    """The splitter itself lives in _process_chat_query; inside this dispatcher a
    compound turn returns None immediately so no single-answer fastpath can
    claim it and drop the tail clause silently. Each clause then re-enters this
    same chain, where the LATE guards apply per clause.
    """
    src = inspect.getsource(getattr(api_server, DISPATCHER))
    tree = ast.parse(re.sub(r"^ +", "", src, count=1) if src.startswith(" ") else src)
    guard_lines = [
        node.lineno for node in ast.walk(tree.body[0])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_is_compound_request"
    ]
    assert guard_lines, (
        "_try_instant_answer no longer stands down on a compound turn; every "
        "helper below it answers ONE thing, so the tail clause is dropped."
    )
    first_hook_line = min(
        lineno for lineno, name in _call_order() if name in EVERY_REAL_COMMAND
    )
    assert min(guard_lines) < first_hook_line, (
        "the compound stand-down must precede every single-answer fastpath; "
        f"guard at line {min(guard_lines)}, first fastpath at {first_hook_line}."
    )


def test_dispatcher_registers_each_hook_exactly_once():
    """A merge that duplicates a registration line makes the order meaningless."""
    names = [name for _lineno, name in _call_order()]
    # _try_capability_lookup is called twice on purpose (early meta-question
    # probe, then the late pass); everything else must appear once.
    expected_repeats = {"_try_capability_lookup": 2}
    counts = {name: names.count(name) for name in set(names)}
    duplicates = {
        name: n for name, n in counts.items()
        if n != expected_repeats.get(name, 1)
    }
    assert not duplicates, f"duplicated hook registrations after merge: {duplicates}"
