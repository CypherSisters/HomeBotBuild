"""Keep the WEEK planner away from single-dish requests, and make it ask first.

Without this guard, the phrase "can you put Tacos on the meal plan for
Thursday dinner" matches ``api_server._MEAL_PLAN_RE``, "plan ... dinner"
inside its 24-character window, and fires ``auto_meal_plan()``.  That tool
rewrites all seven days of ``freshtable.mealPlan`` from a freshly generated
week and then rebuilds the entire ``shoppingList`` from it.  The household's
real plan is replaced, a real shopping list is discarded for a fabricated one,
the named dish never appears anywhere, and the reply says "Dinner plan saved to
FreshTable" with no hint that the whole week was rewritten.  Only a
generational backup gets the household's data back.

Three rules, all enforced here:

1. **A phrase that names ONE thing never reaches the week planner.**  A weekday,
   a demonstrative pointing at a dish, or an add/put/schedule verb are each a
   single-item signal; such a turn belongs to ``meal_writes`` / ``meal_set``.
   The week-planner phrasings ("plan this week's meals", "make me a meal plan
   for the week") carry none of them — "this week" is excluded explicitly,
   because the destructive path must not be re-armed by a possessive.
2. **Replacing a non-empty plan needs an explicit confirmation turn.**  Same
   pending-state shape as the vault display challenge and ``meal_writes``:
   one slot, short TTL (see ``PENDING_TTL``).
3. **The reply says what it replaced.**  A write that silently discards a week
   of the household's decisions is a trust failure even when the new plan is
   good.  The generational backup itself is taken inside ``auto_meal_plan`` so
   that the agent route gets it too; this module only reports it.
"""

from __future__ import annotations

import inspect
import re
import time

_WEEK_ORDER = ("monday", "tuesday", "wednesday", "thursday", "friday",
               "saturday", "sunday")

#: The constraint the ORIGINAL ask carried. "plan the rest of this week's
#: meals, vegetarian" → "yes" must not plan salmon, beef and chicken, which is
#: what happens if the confirmation turn calls the planner with no preferences
#: at all: the word "vegetarian" is only ever in the first turn.
_CONSTRAINT_RE = re.compile(
    r"(?i)\b(vegetarian|vegan|pescatarian|meatless|no\s+meat|"
    r"gluten[-\s]?free|dairy[-\s]?free|nut[-\s]?free|low[-\s]?carb|"
    r"low[-\s]?sodium|high[-\s]?protein|keto|paleo|kosher|halal|"
    r"kid[-\s]?friendly|budget|cheap|quick|slow[-\s]?cooker|"
    r"air[-\s]?fryer|one[-\s]?pot)\b")


def constraint_from(*texts) -> str:
    """Every dietary constraint said across the turns, in order, deduped."""
    out = []
    for text in texts:
        for match in _CONSTRAINT_RE.finditer(str(text or "")):
            phrase = re.sub(r"\s+", " ", match.group(1).lower())
            if phrase not in out:
                out.append(phrase)
    return ", ".join(out)


def _invoke(run, preserve=(), constraint=""):
    """Call the planner, passing only the arguments it actually accepts.

    The fastpath's ``run`` takes ``preserve``/``constraint``; the tests (and
    any older caller) pass a zero-argument callable, and a zero-argument
    callable must keep working exactly as it did.
    """
    try:
        params = inspect.signature(run).parameters
    except (TypeError, ValueError):    # pragma: no cover - builtins only
        params = {}
    kwargs = {}
    if "preserve" in params:
        kwargs["preserve"] = list(preserve)
    if "constraint" in params:
        kwargs["constraint"] = str(constraint or "")
    return run(**kwargs)

#: Long enough to read the confirmation aloud and answer it, short enough that
#: a forgotten prompt cannot be committed by an unrelated "yes" later on.
#:
#: Two minutes. A plain "yes" commits, so the window is the whole exposure.
#: Ten minutes of an
#: unrelated "yes" being able to rewrite the week is more risk than the
#: convenience is worth; the owner answers this question in seconds.
PENDING_TTL = 120.0

_PENDING: dict = {}

_DAYS = ("monday|tuesday|wednesday|thursday|friday|saturday|sunday"
         "|mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun")

#: Every spelling above -> the canonical day key FreshTable stores.
_DAY_FULL = {}
for _full in ("monday", "tuesday", "wednesday", "thursday", "friday",
              "saturday", "sunday"):
    for _form in (_full, _full[:3]):
        _DAY_FULL[_form] = _full
_DAY_FULL.update({"tues": "tuesday", "weds": "wednesday", "thur": "thursday",
                  "thurs": "thursday"})

#: A specific day was named — one slot, not a week.
_NAMES_A_DAY_RE = re.compile(
    r"(?i)\b(?:" + _DAYS + r"|today|tonight|tomorrow)\b")

#: "that", "this", "it" pointing at a dish.  "this week"/"that week" are NOT
#: single-item signals — they are how the legitimate week planner is asked for.
_DEMONSTRATIVE_RE = re.compile(
    r"(?i)\b(?:that|this|these|those|it)\b(?!\s*week|\s*month|\s*weekend)")

#: An add/put/schedule verb takes an OBJECT — one dish going into one slot.
#: "put together" is the week planner's own phrasing and is excluded.
_SINGLE_VERB_RE = re.compile(
    r"(?i)\b(?:add|put(?!\s+together)|schedule|pencil|slot|stick|throw|swap|"
    r"replace\s+(?:the\s+)?\w+\s+(?:dinner|lunch|breakfast))\b")

#: The phrasing the prompt asks for, verbatim.
_CONFIRM_RE = re.compile(
    r"(?i)^\s*(?:yes|yeah|yep|yup|ok|okay|sure|confirm|go\s+ahead)?[\s,.!]*"
    r"(?:please\s+)?replace\s+(?:the|my|our)\s+plan\b")

#: If only the scripted sentence counted as a yes, "Yes, overwrite, but keep
#: Sunday dinner" would be asked the SAME question again. A household answers a
#: yes/no question with a yes. The head of the utterance is what confirms; the
#: tail is read separately for the days to keep.
_PLAIN_CONFIRM_RE = re.compile(
    r"(?i)^\s*(?:yes|yeah|yep|yup|yes\s+please|ok|okay|sure|confirm|confirmed|"
    r"go\s+ahead|do\s+it|please\s+do|go\s+for\s+it|overwrite(?:\s+it)?|"
    r"replace(?:\s+it)?)\b")

#: "but keep Sunday" / "except Sunday" / "leave Sunday alone" — the days the
#: confirmation explicitly EXCLUDES.
_KEEP_DAY_RE = re.compile(
    r"(?i)\b(?:keep|except|excluding|apart\s+from|other\s+than|leave|besides|"
    r"but\s+not|not)\s+(?:the\s+|my\s+|our\s+)?(" + _DAYS + r")\b")

#: The household asked for a REPLACEMENT in so many words. A bare "yes" does
#: not: it answers "plan the rest of this week", and the day already decided
#: stays. Only these words throw the existing days away.
_SAYS_REPLACE_RE = re.compile(
    r"(?i)\b(?:replace|overwrite|start\s+over|from\s+scratch|wipe|redo|"
    r"whole\s+week|all\s+seven|every\s+day)\b")

_DECLINE_RE = re.compile(
    r"(?i)^\s*(?:no|nope|don't|dont|do\s+not|never\s*mind|nevermind|cancel|"
    r"stop|forget\s+it|leave\s+it)\b")


#: A weekday after "starting"/"beginning"/"from" is a START ANCHOR for a WEEK,
#: not a slot: "plan the week starting Monday" asks for the week planner and
#: must reach the confirmation path, not be declined as a single-item turn.
_START_ANCHOR_RE = re.compile(
    r"(?i)\b(?:starting|start(?:s|ing)?\s+(?:on|from)|beginning|begins|"
    r"begin|from)\s+(?:on\s+)?(?:" + _DAYS + r"|today|tomorrow)\b")


def names_a_single_item(text: str) -> bool:
    """True when the phrase points at ONE dish, ONE day or ONE slot.

    Such a turn must never reach ``auto_meal_plan`` — it goes to
    ``meal_writes`` (a recipe from the conversation) or to ``meal_set``.
    """
    low = _START_ANCHOR_RE.sub(" ", str(text or ""))
    return bool(_NAMES_A_DAY_RE.search(low)
                or _DEMONSTRATIVE_RE.search(low)
                or _SINGLE_VERB_RE.search(low))


#: The turn is unmistakably ABOUT the meal plan — it says so, or it is
#: "plan <dish> for <day>". Used only to turn a silent decline into a question.
_SLOT_HINT_RE = re.compile(
    r"(?i)\b(?:meal|dinner|lunch|breakfast|supper|menu)s?\s*plan\b"
    r"|\bplan\b[^.]{0,20}\b(?:meal|meals|dinner|dinners|lunch|breakfast|menu)\b"
    r"|^\s*plan\s+(?!the\b|my\b|our\b|a\b|an\b|this\b|that\b|next\b|out\b|"
    r"some\b|us\b|me\b)[\w'-]+(?:\s+[\w'-]+)?\s+(?:for|on)\s+(?:the\s+)?(?:"
    + _DAYS + r")\b")

SINGLE_ITEM_ASK = ("Which dish and which day? For example: "
                   "\"set Thursday dinner to tacos\".")


def single_item_ask(text: str) -> str | None:
    """Ask, instead of going quiet, when a slot request can't be resolved.

    "put that on Thursday's meal plan", "plan tacos for Thursday" and friends
    can return ``None`` from every meal fastpath, and then the turn falls to
    the agent route, which can still call ``auto_meal_plan`` and rewrite the
    week. A question costs one turn; a silent decline can cost the household
    its plan. Only called once the slot writers have already
    declined, so there is genuinely nothing to write.
    """
    if not names_a_single_item(text):
        return None
    if not _SLOT_HINT_RE.search(str(text or "")):
        return None
    return SINGLE_ITEM_ASK


# ── pending confirmation ───────────────────────────────────────────────────
def get_pending():
    if not _PENDING:
        return None
    if time.time() - _PENDING.get("ts", 0) > PENDING_TTL:
        _PENDING.clear()
        return None
    return dict(_PENDING)


def set_pending(days: int, request: str, constraint: str = "") -> None:
    """Remember the ask, INCLUDING the constraint it carried.

    The agent route reaches the gate through ``auto_meal_plan`` itself, which
    never sees the sentence the household said, only the ``dietary_preferences``
    the model extracted from it. That word is the constraint, and it has to
    survive the confirmation turn or the plan comes back with beef in it.
    """
    _PENDING.clear()
    _PENDING.update({"days": int(days), "request": str(request or ""),
                     "constraint": str(constraint or ""),
                     "ts": time.time()})


def clear_pending() -> None:
    _PENDING.clear()


def _day_count(app) -> int:
    """How many distinct days the current plan covers (0 = empty)."""
    try:
        plan = app._load("freshtable").get("mealPlan") or []
    except Exception:
        return 0
    return len({str(e.get("day", "")).lower() for e in plan
                if isinstance(e, dict) and e.get("day")})


def _app():
    from puffpai.app_data import AppData
    return AppData()


def set_days(app) -> list:
    """The days the stored plan already covers, in week order."""
    try:
        plan = app._load("freshtable").get("mealPlan") or []
    except Exception:
        return []
    present = {str(e.get("day", "")).lower() for e in plan
               if isinstance(e, dict) and e.get("day") and e.get("dish")}
    return [day for day in _WEEK_ORDER if day in present]


def _spoken_days(days) -> str:
    names = [str(d).capitalize() for d in days]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _execute(run, previous_days: int, constraint: str = "") -> str:
    """Run the week planner and say plainly what it did to what was there."""
    return _finish(_invoke(run, (), constraint), previous_days)


def _finish(reply, previous_days: int, fill: bool = False) -> str:
    """The note that goes under whatever the planner returned."""
    if not isinstance(reply, str) or not reply.strip():
        return "The meal plan came back empty — nothing was changed."
    lowered = reply.lower()
    if "came back empty" in lowered or lowered.startswith("i couldn't"):
        return reply
    if fill:
        # Nothing was replaced, so no replacement is announced; the caller
        # names the days that were planned and the days that were kept.
        return f"{reply}\nThe shopping list was rebuilt from the plan."
    if previous_days:
        note = (f"Replaced a {previous_days}-day plan; the previous plan is in "
                f"backup slot 1. The shopping list was rebuilt from the new "
                f"plan.")
    else:
        note = ("Your meal plan was empty, so nothing was replaced. The "
                "shopping list was rebuilt from the new plan.")
    return f"{reply}\n{note}"


def _plan_rows(app, days):
    """The stored rows for *days*, so they can be put back verbatim."""
    try:
        plan = app._load("freshtable").get("mealPlan") or []
    except Exception:
        return []
    wanted = {str(d).lower() for d in days}
    return [dict(row) for row in plan
            if isinstance(row, dict) and str(row.get("day", "")).lower() in wanted]


def _execute_preserving(run, previous_days: int, kept_days, app=None,
                        constraint: str = "", fill: bool = False) -> str:
    """Plan the week, then put the days the household asked to KEEP back.

    "Yes, overwrite, but keep Sunday dinner". Fill-empty-days semantics:
    a day the household has already decided is never overwritten unless the
    confirmation named that day. The week planner rewrites every day it plans,
    so the kept rows are snapshotted first and restored afterwards, and the
    shopping list is rebuilt once more from the restored plan (it is derived
    from the plan, so it must not be left describing a week that was undone).
    """
    if app is None:
        app = _app()
    before = _plan_rows(app, kept_days)
    # The planner is TOLD which days to leave alone; the snapshot below
    # is the belt to that pair of braces, for a planner that ignores it.
    raw = _invoke(run, kept_days, constraint)
    reply = _finish(raw, previous_days, fill)
    lowered = str(reply or "").lower()
    if "came back empty" in lowered or lowered.startswith("i couldn't"):
        return reply
    restored = []
    for row in before:
        try:
            app.fresh_set_meal(row.get("day"), row.get("meal") or "dinner",
                               row.get("dish"), notes=row.get("notes"),
                               recipe_id=row.get("recipeId"))
            restored.append(str(row.get("day", "")).capitalize())
        except Exception:
            continue
    try:
        app.fresh_grocery_from_plan()
    except Exception:
        pass
    kept_names = [d for d in _WEEK_ORDER
                  if d.capitalize() in set(restored)]
    if not kept_names:
        # Nothing was there to keep. Say so rather than claiming a rescue.
        asked = ", ".join(str(d).capitalize() for d in kept_days)
        return f"{reply}\n{asked} had nothing planned, so there was nothing to keep."
    if fill:
        planned = [d for d in set_days(app) if d not in set(kept_names)]
        if planned:
            return (f"{reply}\nPlanned {_spoken_days(planned)}; "
                    f"I left {_spoken_days(kept_names)} exactly as it was.")
    return f"{reply}\nI left {_spoken_days(kept_names)} exactly as it was."


#: Another capability is named, so the turn is a command in its own right and
#: the standing meal-plan question is not what it is answering.
_OTHER_DOMAIN_RE = re.compile(
    r"(?i)\b(?:music|song|songs|playlist|podcast|radio|station|timer|timers|"
    r"alarm|alarms|light|lights|lamp|lamps|fan|thermostat|temperature|heat|"
    r"volume|tv|television|movie|video|news|weather|door|doors|lock|locks|"
    r"vacuum|coffee|reminder|reminders|calendar|shopping|grocery|groceries|"
    r"list|recording|camera|speaker|speakers)\b")

#: The turn is explicitly about the plan, so it IS an answer however long.
_ABOUT_THE_PLAN_RE = re.compile(r"(?i)\b(?:plan|meal|meals|menu|dinners)\b")

#: A yes/no that carries a little tail ("no, leave it alone") is still a bare
#: answer; the tail may only be filler. Anything with real content in it is a
#: command of its own and the pending question keeps waiting.
_ANSWER_HEAD_RE = re.compile(
    r"(?i)^\s*(?:yes|yeah|yep|yup|sure|ok|okay|no|nope|nah|don't|dont|"
    r"do\s+not|never\s*mind|nevermind|cancel|stop|forget\s+it|leave\s+it|"
    r"confirm|go\s+ahead)\b")

_ANSWER_TAIL_RE = re.compile(
    r"(?i)^(?:[\s,.!;:-]|\b(?:please|thanks|thank\s+you|it|that|this|them|"
    r"alone|for\s+now|right\s+now|the|a|my|our|as\s+is|leave|keep|change|"
    r"do|don't|dont|not|anything|nothing|is|are|was|fine|good|great|okay|ok|"
    r"sorry|actually|instead|either|way)\b)*$")


def answers_pending(user_message) -> bool:
    """Is this turn plausibly an answer to the outstanding plan question?

    ``_DECLINE_RE`` matches the FIRST WORD of any sentence, and the pending
    slot is consulted before the intent regex, so without this check, after a
    confirmation prompt "stop the music", "cancel the timer" and "do not turn
    on the lights" would all answer "Okay — I left your meal plan exactly as
    it was." and the real command would be thrown away. A bare
    yes/no is an answer; anything that names another capability is not.
    """
    text = str(user_message or "").strip()
    if not text:
        return False
    if _ABOUT_THE_PLAN_RE.search(text):
        return True
    if _OTHER_DOMAIN_RE.search(text):
        return False
    if len(re.findall(r"[\w']+", text)) <= 3:
        return True
    # "Yes, overwrite, but keep Sunday dinner" opens with a yes and names
    # no other capability, so it IS an answer even though its tail carries a
    # condition the filler list below would never allow.
    if _PLAIN_CONFIRM_RE.match(text):
        return True
    head = _ANSWER_HEAD_RE.match(text)
    return bool(head and _ANSWER_TAIL_RE.match(text[head.end():]))


def keep_days(text) -> list:
    """The days a confirmation explicitly excludes, lower-cased and expanded.

    "yes, overwrite, but keep Sunday dinner" -> ``["sunday"]``.
    """
    out = []
    for match in _KEEP_DAY_RE.finditer(str(text or "")):
        day = _DAY_FULL.get(match.group(1).lower())
        if day and day not in out:
            out.append(day)
    return out


def pending_reply(user_message, run, app=None) -> str | None:
    """Answer a turn that is responding to an outstanding confirmation.

    Returns ``None`` when nothing is pending or the turn is not an answer, so
    the caller falls through to its normal routing — and the pending slot is
    left standing, untouched, for the answer that does arrive.
    """
    pending = get_pending()
    if not pending:
        return None
    if not answers_pending(user_message):
        return None
    text = str(user_message or "").strip()
    # A decline is read FIRST: "no, don't replace the plan" matches the confirm
    # rule's "replace the plan" and must never be committed.
    if _DECLINE_RE.match(text):
        clear_pending()
        return "Okay — I left your meal plan exactly as it was."
    if _CONFIRM_RE.match(text) or _PLAIN_CONFIRM_RE.match(text):
        clear_pending()
        if app is None:
            app = _app()
        # A plain "yes" FILLS. The day the household has already decided
        # ("Sunday dinner: salad and potato bar") is not what they are
        # agreeing to lose; they are answering "plan the rest of this week".
        # Only the scripted "replace the plan" wording still replaces.
        kept = keep_days(text)
        fill = not kept and not _SAYS_REPLACE_RE.search(text)
        if fill:
            kept = set_days(app)
        # …and the constraint the ORIGINAL ask carried comes with it.
        constraint = (pending.get("constraint")
                      or constraint_from(pending.get("request", ""), text))
        extra = constraint_from(text)
        if extra and extra not in constraint:
            constraint = ", ".join(x for x in (constraint, extra) if x)
        if kept:
            return _execute_preserving(run, pending.get("days", 0), kept, app,
                                       constraint=constraint, fill=fill)
        return _execute(run, pending.get("days", 0), constraint)
    return None


def plan_reply(user_message, run, app=None) -> str | None:
    """Decide what a matched week-plan command is allowed to do.

    ``None`` means "not the week planner's turn" — the single-item guard.
    """
    if names_a_single_item(user_message):
        return None
    if app is None:
        app = _app()
    previous_days = _day_count(app)
    if previous_days:
        set_pending(previous_days, user_message,
                    constraint_from(user_message))
        return (f"This will replace your current {previous_days}-day plan and "
                f"rebuild the shopping list. Say 'yes, replace the plan' to "
                f"continue.")
    return _execute(run, 0, constraint_from(user_message))
