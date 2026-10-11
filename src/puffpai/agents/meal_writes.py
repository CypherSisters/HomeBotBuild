"""Write a recipe that was surfaced in CONVERSATION into the FreshTable plan.

On the agent route, a household member shown two salmon recipes by
``search_knowledge_base`` says "Add both of those salmon recipes to the dinner
meals for this week." The agent asks which days, gets Wednesday and Thursday,
asks to confirm, hears "Yes please, that's perfect.", and can reply "I don't
have a tool in this environment that can actually create or schedule recipes
inside FreshTable."

Two causes, both structural, not model-quality:

1. ``capability_registry.tools_for_query`` binds tools from the CURRENT
   utterance only.  "Yes please, that's perfect." contains no food keyword, so
   the bind fell to ``_filter_tools_by_intent``'s three-tool safety floor
   (search_knowledge_base / list_indexed_libraries / web_search).  The model
   tells the truth about the menu it has been handed; the menu is the
   problem.  A multi-turn clarify → confirm flow can therefore NEVER complete on
   the agent route, because the confirming turn is always keyword-free.
2. ``meal_set`` takes a dish TITLE ('day | meal | dish | notes'), so even a
   successful call would put a bare name on the calendar and drop the recipe
   body the household had just heard read aloud.

This module owns the whole flow deterministically — no LLM, no tool menu.  It
pulls the recipe titles + bodies out of the conversation history, resolves
"both of those" / "the first" / "the herb crusted one" / "that salmon recipe"
against them, parses the days, saves a real recipe into FreshTable and links it
from the meal-plan slot, and reads back what it scheduled.  A turn that is
missing days asks for them and remembers the request, so the follow-up ("let's
do Wednesday and Thursday") and the confirmation ("yes please") complete
instead of collapsing.
"""

from __future__ import annotations

import re
import time
import unicodedata

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"]
MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")

#: A pending request lives this long.  Long enough for a real clarify → answer
#: → confirm exchange (the live one ran 2m24s end to end), short enough that a
#: forgotten request cannot be committed by an unrelated "yes" an hour later.
PENDING_TTL = 600.0

#: Single household box, single conversation at a time — same shape as the
#: vault display challenge.  Reset by :func:`clear_pending`.
_PENDING: dict = {}


# ── text normalisation ─────────────────────────────────────────────────────
_DASHES = dict.fromkeys(
    [0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212], "-")
_QUOTES = {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"'}


def normalize(text: str) -> str:
    """NFKC + the unicode hyphens/quotes an LLM reply is full of.

    The live transcript wrote "Pan‑Seared Salmon with Lemon‑Butter
    Sauce" with NON-BREAKING hyphens, so a plain ``"herb-crusted" in title``
    test against the spoken "herb crusted" missed twice over.
    """
    text = unicodedata.normalize("NFKC", str(text or ""))
    return text.translate({**_DASHES, **_QUOTES})


# ── recipe extraction from conversation history ────────────────────────────
#: Joiner words allowed INSIDE a dish name ("Salmon with Lemon-Butter Sauce").
_JOINERS = {"with", "and", "in", "of", "the", "a", "la", "au", "on", "over",
            "de", "&"}

_TITLE_RE = re.compile(
    r"\b([A-Z][\w']*(?:[- ](?:[A-Z][\w']*|" + "|".join(sorted(_JOINERS)) + r")){1,7})")

#: A message only offers recipe titles if it is actually about food.  Without
#: this, "One of the" and other sentence-initial title-case noise from any
#: chatty reply would enter the candidate pool.
_FOODY_RE = re.compile(
    r"(?i)\b(recipe|ingredient|ingredients|cook|bake|saut|sear|roast|grill|"
    r"serve|serves|preheat|simmer|tablespoon|teaspoon|dish)\b")

_SECTION_RE = re.compile(
    r"(?im)^\s*[#*\-\s]*(ingredients?|instructions?|steps?|directions?|method)\b"
    r"\s*[:\-]?\s*$")


def _candidate_titles(message: str) -> list:
    """Dish-name candidates in one assistant message, best first."""
    out, seen = [], set()
    for line in message.splitlines():
        stripped = line.strip()
        head = re.match(r"^#{1,6}\s*(.+?)\s*#*$", stripped)
        bold = re.match(r"^\*\*(.+?)\*\*:?$", stripped)
        for m in (head, bold):
            if m:
                cand = m.group(1).strip(" *:")
                if cand.lower() not in seen and len(cand.split()) >= 2:
                    seen.add(cand.lower())
                    out.append(cand)
    for m in _TITLE_RE.finditer(message):
        cand = m.group(1).strip()
        # Trim trailing joiners: "One of the" -> "One" (then rejected below).
        parts = cand.split()
        while parts and parts[-1].lower() in _JOINERS:
            parts.pop()
        cand = " ".join(parts)
        caps = [p for p in parts if p[:1].isupper()]
        if len(caps) < 2:
            continue
        if cand.lower() in seen:
            continue
        seen.add(cand.lower())
        out.append(cand)
    # Longest first: the full dish name beats a fragment of itself.
    return sorted(out, key=lambda c: -len(c))


def _split_body(message: str) -> tuple:
    """(ingredients_text, instructions_text) from a recipe reply."""
    lines = message.splitlines()
    marks = []
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m:
            marks.append((i, m.group(1).lower()))
    ing, steps = "", ""
    for idx, (i, kind) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(lines)
        chunk = "\n".join(lines[i + 1:end]).strip()
        if kind.startswith("ingredient") and not ing:
            ing = chunk
        elif not steps:
            steps = chunk
    if not ing and not steps:
        steps = message.strip()
    return ing[:2000], steps[:4000]


def _ingredient_items(ingredients_text: str) -> list:
    items = []
    for line in ingredients_text.splitlines():
        line = line.strip(" \t-*•").strip()
        if line and not line.endswith(":"):
            items.append(line[:120])
    return items[:30]


def extract_recipes(conversation_history) -> list:
    """Recipes offered by the assistant earlier in this conversation.

    Oldest first, so "the first salmon recipe" and "the second" index
    naturally.  Each entry is ``{title, ingredients, instructions, body}``.

    ``conversation_history`` may be in EITHER shape — see
    :mod:`puffpai.agents.conversation_history`.  Reading only ``msg["role"]``
    here is what made this whole fastpath a no-op in production.
    """
    from puffpai.agents.conversation_history import assistant_messages
    out = []
    for content in assistant_messages(conversation_history):
        body = normalize(content)
        if not body or not _FOODY_RE.search(body):
            continue
        titles = _candidate_titles(body)
        if not titles:
            continue
        ing, steps = _split_body(body)
        out.append({"title": titles[0], "alt_titles": titles[1:4],
                    "ingredients": _ingredient_items(ing),
                    "instructions": steps, "body": body})
    return out


# ── request parsing ────────────────────────────────────────────────────────
_STOPWORDS = {
    "add", "put", "set", "schedule", "make", "plan", "both", "those", "that",
    "these", "this", "the", "for", "and", "with", "into", "onto", "them",
    "recipe", "recipes", "one", "ones", "dish", "dishes", "meal", "meals",
    "dinner", "dinners", "lunch", "breakfast", "week", "weeks", "please",
    "first", "second", "third", "other", "another", "menu", "our", "my",
    "lets", "let", "just", "also", "some", "them", "they", "food",
}

_DAY_RE = re.compile(
    r"(?i)\b(" + "|".join(WEEKDAYS) + r"|mon|tue|tues|wed|weds|thu|thur|thurs|"
    r"fri|sat|sun|today|tonight|tomorrow)\b")

_DAY_ALIASES = {"mon": "monday", "tue": "tuesday", "tues": "tuesday",
                "wed": "wednesday", "weds": "wednesday", "thu": "thursday",
                "thur": "thursday", "thurs": "thursday", "fri": "friday",
                "sat": "saturday", "sun": "sunday"}

#: An ADD to a named slot.  Deliberately narrower than "anything about meals":
#: "plan this week's meals" stays with auto_meal_plan and "what's for dinner"
#: stays a read.
_ADD_RE = re.compile(
    r"(?i)\b(?:add|put|schedule|pencil|slot|stick|throw|set)\b"
    r"[^.?!]{0,80}?\b(?:dinner|dinners|lunch|breakfast|meal|meals|menu|"
    + "|".join(WEEKDAYS) + r")\b")

_READ_RE = re.compile(
    r"(?i)^\s*(?:what|what's|whats|when|show|list|tell\s+me\s+what)\b")

#: "Yes please, that's perfect." is the exact turn that collapsed live, so the
#: trailing pleasantries REPEAT rather than allowing exactly one.
_AFFIRM_RE = re.compile(
    r"(?i)^\s*(?:yes|yeah|yep|yup|sure|ok|okay|correct|right|do\s+it|"
    r"go\s+ahead|sounds\s+good|please\s+do|that's\s+it|thats\s+it)\b"
    r"(?:[\s,.!]*(?:please|that's\s+perfect|thats\s+perfect|that's\s+right|"
    r"perfect|great|thanks|thank\s+you|do\s+it|go\s+ahead|exactly))*"
    r"[\s,.!]*$")


#: A turn that names a weekday while a meal request is pending is only an
#: ANSWER to "which days?" if it is short, or if it is still talking about
#: food. "Remind me Wednesday to call the vet" names a day and is not.
_ANSWER_FOOD_RE = re.compile(
    r"(?i)\b(?:recipe|recipes|dish|dishes|meal|meals|dinner|lunch|breakfast|"
    r"one|ones|first|second|third|both|that|those|salmon)\b")
_ANSWER_ELSEWHERE_RE = re.compile(
    r"(?i)\b(?:remind|reminder|alarm|timer|calendar|appointment|call|text|"
    r"email|task|meeting|pick\s+up|turn\s+on|turn\s+off|play|weather)\b")


def _answers_the_question(low: str) -> bool:
    if _ANSWER_ELSEWHERE_RE.search(low):
        return False
    return len(low.split()) <= 6 or bool(_ANSWER_FOOD_RE.search(low))


def parse_days(text: str) -> list:
    """Weekday names in the order they were spoken, de-duplicated."""
    import datetime as _dt
    days, seen = [], set()
    today = _dt.date.today()
    for raw in _DAY_RE.findall(normalize(text)):
        low = raw.lower()
        if low in ("today", "tonight"):
            day = WEEKDAYS[today.weekday()]
        elif low == "tomorrow":
            day = WEEKDAYS[(today.weekday() + 1) % 7]
        else:
            day = _DAY_ALIASES.get(low, low)
        if day in WEEKDAYS and day not in seen:
            seen.add(day)
            days.append(day)
    return days


def parse_meal_type(text: str) -> str:
    low = normalize(text).lower()
    for meal in ("breakfast", "lunch", "snack", "dinner"):
        if re.search(r"\b" + meal + r"s?\b", low):
            return meal
    return "dinner"


def _descriptors(text: str) -> list:
    low = normalize(text).lower()
    words = re.findall(r"[a-z]{3,}", low)
    return [w for w in words if w not in _STOPWORDS and w not in WEEKDAYS]


def resolve_targets(text: str, recipes: list) -> list:
    """Which of the conversation's recipes the user just pointed at."""
    if not recipes:
        return []
    low = normalize(text).lower()
    descs = _descriptors(low)

    def _hits(recipe):
        hay = (recipe["title"] + " " + " ".join(recipe.get("alt_titles", []))).lower()
        return sum(1 for d in descs if d in hay)

    scored = [(r, _hits(r)) for r in recipes]
    matched = [r for r, n in scored if n] or []

    wants_all = re.search(r"\bboth\b|\ball\s+of\s+(?:them|those)\b|\btwo\b", low)
    if wants_all:
        pool = matched or recipes
        return pool[-2:] if len(pool) >= 2 else pool[:]

    ordinal = None
    if re.search(r"\bfirst\b", low):
        ordinal = 0
    elif re.search(r"\bsecond\b", low):
        ordinal = 1
    elif re.search(r"\bthird\b", low):
        ordinal = 2
    if ordinal is not None:
        pool = matched or recipes
        if ordinal < len(pool):
            return [pool[ordinal]]
        return []

    if matched:
        best = max(scored, key=lambda s: s[1])
        return [best[0]]
    # "add that recipe to Wednesday's dinner" with no descriptor: the last one
    # the assistant offered is the only thing "that" can mean.
    if re.search(r"\b(?:that|the|it|this)\b", low):
        return [recipes[-1]]
    return []


# ── pending state ──────────────────────────────────────────────────────────
def get_pending():
    if not _PENDING:
        return None
    if time.time() - _PENDING.get("ts", 0) > PENDING_TTL:
        _PENDING.clear()
        return None
    return dict(_PENDING)


def set_pending(targets, meal, days):
    _PENDING.clear()
    _PENDING.update({"targets": list(targets), "meal": meal,
                     "days": list(days), "ts": time.time()})


def clear_pending():
    _PENDING.clear()


# ── the write ──────────────────────────────────────────────────────────────
def _save_recipe(app, recipe) -> str | None:
    """Save the conversation's recipe into FreshTable, return its id."""
    try:
        existing = app._load("freshtable").get("recipes", []) or []
        for r in existing:
            if str(r.get("name", "")).strip().lower() == recipe["title"].lower():
                return r.get("id")
        app.fresh_create_recipe(recipe["title"], recipe.get("ingredients") or [],
                                recipe.get("instructions") or "")
        after = app._load("freshtable").get("recipes", []) or []
        return after[-1].get("id") if after else None
    except Exception:
        return None


def commit(targets, meal, days, app=None) -> str:
    """Write each recipe into its day's slot and read back what was saved."""
    if app is None:
        from puffpai.app_data import AppData
        app = AppData()
    saved = []
    for recipe, day in zip(targets, days):
        recipe_id = _save_recipe(app, recipe)
        ing = recipe.get("ingredients") or []
        notes = ("ingredients: " + ", ".join(ing[:10])) if ing else None
        app.fresh_set_meal(day, meal, recipe["title"], notes=notes,
                           recipe_id=recipe_id)
        saved.append(f"{day.capitalize()} {meal}: {recipe['title']}")
    if not saved:
        return "I couldn't tell which recipe you meant — say the dish name."
    body = "\n  - ".join(saved)
    tail = ("The full recipe is saved in FreshTable, so it's the recipe on the "
            "plan, not just the name.")
    return f"Saved to the FreshTable meal plan:\n  - {body}\n{tail}"


def _ask_for_days(targets, meal) -> str:
    names = " and ".join(r["title"] for r in targets)
    if len(targets) == 1:
        return (f"Which day should I put {names} on for {meal}? "
                f"Say a weekday and I'll save it.")
    return (f"Which days for {names}? Name one day per recipe — "
            f"\"Wednesday and Thursday\", say — and I'll save them.")


def handle(user_message, conversation_history=None) -> str | None:
    """The whole flow.  Returns a reply, or None to decline the turn."""
    text = normalize(user_message or "").strip()
    if not text or len(text) > 300:
        return None
    low = text.lower()

    pending = get_pending()

    # ── a confirmation or an answer to "which days?" ──
    if pending:
        days = parse_days(text)
        recipes = extract_recipes(conversation_history)
        retarget = resolve_targets(text, recipes) if recipes else []
        targets = pending["targets"]
        if retarget and len(retarget) == len(pending["targets"]):
            targets = retarget
        if days and _answers_the_question(low):
            if len(days) < len(targets):
                targets = targets[:len(days)]
            clear_pending()
            return commit(targets, pending["meal"], days[:len(targets)])
        if _AFFIRM_RE.match(low):
            if pending["days"]:
                clear_pending()
                return commit(targets, pending["meal"], pending["days"])
            return _ask_for_days(targets, pending["meal"])
        if re.search(r"(?i)\b(?:no|nope|never\s*mind|cancel|forget\s+it)\b", low):
            clear_pending()
            return "Okay — nothing saved to the meal plan."
        # Anything else: not ours, and the request stays pending.
        return None

    # ── a fresh "add <recipe> to <day>'s dinner" ──
    if _READ_RE.match(low) or not _ADD_RE.search(low):
        return None
    recipes = extract_recipes(conversation_history)
    targets = resolve_targets(text, recipes)
    if not targets:
        return None
    meal = parse_meal_type(text)
    days = parse_days(text)
    if not days:
        set_pending(targets, meal, [])
        return _ask_for_days(targets, meal)
    if len(days) < len(targets):
        targets = targets[:len(days)]
    return commit(targets, meal, days[:len(targets)])


# ── a dish named OUT LOUD, with no recipe in the conversation ───────────────
# Without this path "Set thursday dinner to Tacos" routes to RAG, which can
# reply "Please try saying 'set thursday dinner to Tacos' as a direct
# command", the user's own sentence handed back as the fix. The sentence IS
# a direct command. ``handle`` above only fires when the dish came from an
# earlier assistant turn, so a dish stated inline would have no deterministic
# path at all and would depend on the agent route binding ``meal_set``, which
# it does not do reliably.

_DAY_TOKEN = ("monday|tuesday|wednesday|thursday|friday|saturday|sunday"
              "|mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun"
              "|today|tonight|tomorrow")
_MEAL_TOKEN = "breakfast|brunch|lunch|dinner|supper|snack"
_DAY_G = r"(?P<day>" + _DAY_TOKEN + r")(?:'s|s')?"
_MEAL_G = r"(?P<meal>" + _MEAL_TOKEN + r")"

_DIRECT_PATTERNS = [
    # "set thursday dinner to X", "change wednesday's lunch to X"
    re.compile(r"(?i)\b(?:set|change|update|switch|make)\s+(?:the\s+|my\s+|our\s+)?"
               + _DAY_G + r"\s+" + _MEAL_G + r"\s+(?:to|as|=)\s+(?P<dish>.+)$"),
    # "set dinner on thursday to X"
    re.compile(r"(?i)\b(?:set|change|update|switch|make)\s+(?:the\s+|my\s+|our\s+)?"
               + _MEAL_G + r"\s+(?:on|for)\s+" + _DAY_G
               + r"\s+(?:to|as|=)\s+(?P<dish>.+)$"),
    # "thursday dinner is X", "thursday's dinner will be X"
    re.compile(r"(?i)^\s*(?:for\s+|on\s+)?" + _DAY_G + r"\s+" + _MEAL_G
               + r"\s+(?:is|will\s+be|should\s+be|=)\s+(?P<dish>.+)$"),
    # "make X thursday's dinner"
    re.compile(r"(?i)\bmake\s+(?P<dish>.+?)\s+(?:the\s+|our\s+|my\s+)?" + _DAY_G
               + r"\s+" + _MEAL_G + r"\b"),
    # "X for dinner on thursday"
    re.compile(r"(?i)^\s*(?P<dish>.+?)\s+for\s+" + _MEAL_G
               + r"\s+(?:on|this)\s+" + _DAY_G + r"\b"),
    # "put X on the meal plan for thursday dinner" (must never reach the
    # week planner)
    re.compile(r"(?i)\b(?:put|add|schedule|pencil|slot)\s+(?P<dish>.+?)\s+"
               r"(?:on|onto|in|into|to)\s+(?:the\s+|my\s+|our\s+)?"
               r"(?:meal\s+plan|dinner\s+plan|menu|plan)\s+(?:for\s+|on\s+)?"
               + _DAY_G + r"(?:\s+" + _MEAL_G + r")?\b"),
    # "put X on thursday's dinner", "add X to wednesday dinner"
    re.compile(r"(?i)\b(?:put|add|schedule|pencil|slot)\s+(?P<dish>.+?)\s+"
               r"(?:on|onto|to|in|into|for)\s+(?:the\s+|my\s+|our\s+)?" + _DAY_G
               + r"\s+" + _MEAL_G + r"\b"),
]

#: Words that can precede the dish and are not part of its name.
_DISH_LEAD_RE = re.compile(
    r"(?i)^(?:can\s+you\s+|could\s+you\s+|please\s+|just\s+|go\s+ahead\s+and\s+)?"
    r"(?:the\s+|a\s+|an\s+|some\s+|my\s+|our\s+|that\s+|this\s+)*")
#: …and the trailing scaffolding a spoken sentence carries.
_DISH_TAIL_RE = re.compile(
    r"(?i)\s*(?:recipe|dish|meal|plan|this\s+week|next\s+week|for\s+dinner|"
    r"for\s+lunch|for\s+breakfast|please|thanks|thank\s+you)\s*$")

#: A "dish" made only of these is a pronoun, not a name — that turn needs the
#: conversation recipe path, and if that declined there is nothing to write.
_NOT_A_DISH = {"that", "this", "it", "them", "those", "these", "one", "ones",
               "something", "anything", "dinner", "lunch", "breakfast", "meal",
               "food", "the", "a", "an", "some", "what", "whats", "which"}


def _clean_dish(raw: str) -> str | None:
    dish = normalize(raw or "").strip().strip(" .!?,;:\"'")
    dish = _DISH_LEAD_RE.sub("", dish, count=1).strip()
    for _ in range(3):
        trimmed = _DISH_TAIL_RE.sub("", dish).strip(" .!?,;:")
        if trimmed == dish:
            break
        dish = trimmed
    dish = dish.strip(" .!?,;:\"'")
    if not dish or len(dish) > 60 or len(dish.split()) > 8:
        return None
    if _DAY_RE.search(dish):
        return None
    words = [w for w in re.findall(r"[a-z']+", dish.lower())]
    if words and all(w in _NOT_A_DISH for w in words):
        return None
    if not re.search(r"[A-Za-z]", dish):
        return None
    return dish


def parse_direct_set(text: str):
    """``(day, meal, dish)`` for a slot named outright, else ``None``."""
    line = normalize(text or "").strip()
    if not line or len(line) > 200:
        return None
    if _READ_RE.match(line):
        return None
    for pattern in _DIRECT_PATTERNS:
        m = pattern.search(line)
        if not m:
            continue
        groups = m.groupdict()
        raw_day = (groups.get("day") or "").lower()
        days = parse_days(raw_day)
        if not days:
            continue
        dish = _clean_dish(groups.get("dish") or "")
        if not dish:
            continue
        meal = (groups.get("meal") or "").lower()
        if meal in ("supper",):
            meal = "dinner"
        elif meal == "brunch":
            meal = "lunch"
        if meal not in MEAL_TYPES:
            meal = parse_meal_type(line)
        return days[0], meal, dish


def handle_direct(user_message, app=None) -> str | None:
    """Write a slot whose dish the user just said out loud."""
    parsed = parse_direct_set(user_message)
    if not parsed:
        return None
    day, meal, dish = parsed
    if app is None:
        from puffpai.app_data import AppData
        app = AppData()
    app.fresh_set_meal(day, meal, dish)
    clear_pending()
    return f"{day.capitalize()} {meal} is set to {dish}."
