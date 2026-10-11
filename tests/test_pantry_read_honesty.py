"""Pantry read: "do we have rice in the pantry" must not answer "no" over real rice.

If ``AppData.fresh_list_pantry`` compared the filter word against
``item['category']`` only, "rice" would be searched against "Grains" and find
nothing, and the agent that called ``app_list_pantry`` with "rice" would report
"no rice" while the pantry holds three real Rice entries.

Two guards:

* the reader matches item NAME as well as category, case-insensitively and
  singular/plural-tolerantly, on whole words; and
* a miss says what it searched, so an empty filter result cannot be dressed
  up into a confident denial over real stock.

The fastpath rules in ``api_server._CAPREAD_RULES`` take the same turns
deterministically, so the answer does not depend on the model picking the
right tool with the right argument.
"""

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.app_data import AppData  # noqa: E402
import puffpai.api_server as api  # noqa: E402

#: A freshtable fixture carrying BOTH shapes the bug confused: items whose
#: NAME is the searched word (Rice) filed under an unrelated category, and
#: items reachable only by CATEGORY (Cheddar under Dairy).
FIXTURE = {
    "pantry": [
        {"name": "Rice", "category": "Grains", "quantity": 4,
         "unit": "bags", "maxQuantity": 12},
        {"name": "Brown Rice", "category": "Grains", "quantity": 2,
         "unit": "bags", "maxQuantity": 6},
        {"name": "Wild Rice", "category": "Grains", "quantity": 1,
         "unit": "box", "maxQuantity": 6},
        {"name": "Whole Milk", "category": "Dairy", "quantity": 0,
         "unit": "gallons", "maxQuantity": 3},
        {"name": "Cheddar", "category": "Dairy", "quantity": 2,
         "unit": "blocks", "maxQuantity": 4},
        {"name": "Tomatoes", "category": "Produce", "quantity": 5,
         "unit": "pcs", "maxQuantity": 10},
    ],
    "shoppingList": [],
    "mealPlan": [],
}


class _FixtureStore(AppData):
    """AppData reading one in-memory freshtable. No file, no state dir."""

    def __init__(self, data):
        self._locks = {"freshtable": threading.Lock()}
        self._data = data

    def _load(self, name):
        return self._data


@pytest.fixture
def pantry(tmp_path):
    # Round-tripped through JSON so the fixture is exactly what a real
    # freshtable.json would deserialize to.
    path = tmp_path / "freshtable.json"
    path.write_text(json.dumps(FIXTURE), encoding="utf-8")
    return _FixtureStore(json.loads(path.read_text(encoding="utf-8")))


# ── the rice turn ───────────────────────────────────────────────────────────

def test_name_match_finds_every_rice(pantry):
    said = pantry.fresh_list_pantry("rice")
    assert "Rice: 4 bags" in said
    assert "Brown Rice: 2 bags" in said
    assert "Wild Rice: 1 box" in said
    assert "don't see" not in said


def test_name_match_is_case_insensitive(pantry):
    assert "Rice: 4 bags" in pantry.fresh_list_pantry("Rice")


def test_quantity_and_units_are_reported(pantry):
    """"how much rice do we have" has to come back with numbers and units."""
    said = pantry.fresh_list_pantry("rice")
    for expected in ("4 bags", "2 bags", "1 box"):
        assert expected in said


def test_singular_query_matches_plural_stock(pantry):
    assert "Tomatoes: 5 pcs" in pantry.fresh_list_pantry("tomato")


def test_plural_query_matches_singular_stock(pantry):
    assert "Rice: 4 bags" in pantry.fresh_list_pantry("rices")


def test_category_lookups_still_work(pantry):
    said = pantry.fresh_list_pantry("dairy")
    assert "Whole Milk" in said and "Cheddar" in said
    assert "Rice" not in said


def test_out_of_milk_answers_from_real_inventory(pantry):
    """Zero on hand is a real answer, not a miss: the item IS listed."""
    said = pantry.fresh_list_pantry("milk")
    assert "Whole Milk: 0 gallons" in said
    assert "don't see" not in said


def test_word_boundary_stops_a_substring_false_positive():
    assert not AppData._pantry_item_matches(
        {"name": "Liquorice", "category": "Candy"}, "rice")


# ── honest misses ────────────────────────────────────────────────────────────

def test_unknown_item_says_what_was_searched(pantry):
    said = pantry.fresh_list_pantry("quinoa")
    low = said.lower()
    assert "quinoa" in low
    assert "pantry" in low
    assert "6 item" in low            # names the size of what it searched
    assert "categories" in low


def test_a_miss_never_invents_an_item(pantry):
    said = pantry.fresh_list_pantry("quinoa")
    for real in ("Rice", "Cheddar", "Tomatoes"):
        assert real not in said


def test_low_stock_miss_also_says_what_was_checked():
    stocked = _FixtureStore({"pantry": [
        {"name": "Rice", "category": "Grains", "quantity": 10,
         "unit": "bags", "maxQuantity": 12}]})
    said = stocked.fresh_list_pantry("low")
    assert "1 item" in said and "checked" in said.lower()


def test_empty_pantry_is_unchanged():
    assert _FixtureStore({"pantry": []}).fresh_list_pantry("rice") == \
        "Pantry is empty."


# ── deterministic dispatch (api_server._CAPREAD_RULES) ───────────────────────

def _capread(text):
    """The (tool, captured argument) the rules table would dispatch."""
    for name, match_re, exclude_re, needs_query in api._CAPREAD_RULES:
        m = match_re.search(text)
        if not m:
            continue
        if exclude_re is not None and exclude_re.search(text):
            continue
        if not needs_query:
            return name, ""
        arg = ""
        for group in ("q", "q2"):
            if group in match_re.groupindex and m.group(group):
                arg = m.group(group)
                break
        arg = arg.strip(" .?!,\"'")
        if not arg:
            continue
        return name, arg
    return None, None


@pytest.mark.parametrize("text,item", [
    ("do we have rice in the pantry", "rice"),
    ("do we have any rice in the pantry", "rice"),
    ("do we still have brown rice in the pantry", "brown rice"),
    ("is there any milk in the fridge", "milk"),
    ("are we out of milk", "milk"),
    ("how much rice do we have", "rice"),
    ("how many eggs do we have", "eggs"),
])
def test_item_lookups_dispatch_with_the_item_name(text, item):
    assert _capread(text) == ("app_list_pantry", item), text


def test_the_browse_read_still_dispatches_with_no_filter():
    assert _capread("whats in the pantry") == ("app_list_pantry", "")


@pytest.mark.parametrize("text", [
    "how much time do we have",
    "how many events do we have",
    "how many reminders do we have",
    "how much money do we have",
    "are we out of time",
    "are we out of luck",
    "how many photos do we have",
    "how much water do we have",
    "how much food do we have",
    "how many days of food do we have",
    "how many recipes do we have",
    "what do we need from the store",
])
def test_other_domains_are_not_answered_from_the_pantry(text):
    assert _capread(text)[0] != "app_list_pantry", text


@pytest.mark.parametrize("text", [
    "add rice to the pantry",
    "remove the rice from the pantry",
])
def test_pantry_writes_keep_their_own_path(text):
    assert _capread(text)[0] != "app_list_pantry", text


# ── determiners, and the bare lookup that must reach the reader ─────────────
#
# Capture groups that strip only any|some|more|enough leave the four commonest
# English determiners INSIDE the capture: "do we have an egg in the fridge"
# would search the pantry for the literal string "an egg" and answer
# `I don't see "an egg" in the pantry` over twelve real eggs. A deterministic
# false denial is strictly worse than leaving the turn to the model.
DETERMINER_LOOKUPS = [
    ("do we have an egg in the fridge", "egg"),
    ("do we have a tomato in the pantry", "tomato"),
    ("do we have the flour in the pantry", "flour"),
    ("how much of the rice do we have", "rice"),
    ("how many of the eggs do we have", "eggs"),
    ("is there the milk in the fridge", "milk"),
    ("are we out of the milk", "milk"),
]

#: Bare stock questions naming no room and carrying no "how much" frame must
#: reach the reader too.
BARE_LOOKUPS = [
    ("do we have rice", "rice"),
    ("do we have any eggs", "eggs"),
    ("do we have milk", "milk"),
    ("do we have any flour", "flour"),
    ("is there any milk", "milk"),
    ("got any eggs", "eggs"),
    ("do we have an egg", "egg"),
]

#: The bare rule is the widest thing in the table, so its exclude is pinned
#: here one domain at a time. None of these is a pantry question.
BARE_MUST_NOT_STEAL = [
    "do we have any events",
    "do we have any appointments",
    "do we have any bills",
    "do we have any medications",
    "do we have any meds",
    "do we have any alarms",
    "do we have any timers",
    "do we have any reminders",
    "do we have any cameras",
    "do we have any lights",
    "do we have any devices",
    "do we have any recipes",
    "do we have any photos",
    "do we have any documents",
    "do we have any supplies",
    "do we have time",
    "do we have money",
    "do we have anything",
    "is there anyone here",
    "do we have any tasks",
]


@pytest.mark.parametrize("text,item", DETERMINER_LOOKUPS)
def test_determiners_are_stripped_from_the_capture(text, item):
    assert _capread(text) == ("app_list_pantry", item), text


@pytest.mark.parametrize("text,item", BARE_LOOKUPS)
def test_a_bare_stock_question_reaches_the_reader(text, item):
    assert _capread(text) == ("app_list_pantry", item), text


@pytest.mark.parametrize("text", BARE_MUST_NOT_STEAL)
def test_the_bare_rule_steals_no_neighbouring_domain(text):
    assert _capread(text)[0] != "app_list_pantry", text


@pytest.mark.parametrize("text,item", DETERMINER_LOOKUPS + BARE_LOOKUPS)
def test_the_determined_phrasing_finds_the_real_stock(text, item):
    """End to end: each phrasing, against real stock."""
    store = _FixtureStore({"pantry": [
        {"name": "Eggs", "category": "Dairy", "quantity": 12,
         "unit": "count", "maxQuantity": 24},
        {"name": "Tomatoes", "category": "Produce", "quantity": 5,
         "unit": "pcs", "maxQuantity": 10},
        {"name": "Flour", "category": "Baking", "quantity": 2,
         "unit": "bags", "maxQuantity": 4},
        {"name": "Rice", "category": "Grains", "quantity": 4,
         "unit": "bags", "maxQuantity": 12},
        {"name": "Whole Milk", "category": "Dairy", "quantity": 1,
         "unit": "gallons", "maxQuantity": 3},
    ]})
    said = store.fresh_list_pantry(_capread(text)[1])
    assert "i don't see" not in said.lower(), (text, said)


@pytest.mark.parametrize("term", [
    "an egg", "a tomato", "the flour", "of the rice", "any eggs",
    "some milk", "our rice", "my flour",
])
def test_the_reader_strips_determiners_defensively(term):
    """The tool is also reachable from the agent, from voice and from the app,
    where nothing stripped the determiner first."""
    store = _FixtureStore({"pantry": [
        {"name": "Eggs", "category": "Dairy", "quantity": 12,
         "unit": "count", "maxQuantity": 24},
        {"name": "Tomatoes", "category": "Produce", "quantity": 5,
         "unit": "pcs", "maxQuantity": 10},
        {"name": "Flour", "category": "Baking", "quantity": 2,
         "unit": "bags", "maxQuantity": 4},
        {"name": "Rice", "category": "Grains", "quantity": 4,
         "unit": "bags", "maxQuantity": 12},
        {"name": "Whole Milk", "category": "Dairy", "quantity": 1,
         "unit": "gallons", "maxQuantity": 3},
    ]})
    said = store.fresh_list_pantry(term)
    assert "i don't see" not in said.lower(), (term, said)


def test_a_bare_determiner_is_still_an_honest_miss():
    """Stripping must not turn "the" into "match everything"."""
    store = _FixtureStore({"pantry": [
        {"name": "Rice", "category": "Grains", "quantity": 4,
         "unit": "bags", "maxQuantity": 12}]})
    assert AppData._pantry_term_variants("the") == []
    assert "i don't see" in store.fresh_list_pantry("the").lower()


def test_the_browse_read_is_still_unclaimed_by_the_bare_rule():
    assert _capread("whats in the pantry") == ("app_list_pantry", "")


#: Nouns that share the bare rule's SHAPE and are
#: not groceries. An honest `I don't see "printer" in the pantry` is still a
#: stolen turn, and "do we have school tomorrow" is a calendar question.
BARE_NOT_GROCERIES = [
    "do we have a dog",
    "do we have a plan",
    "do we have a minute",
    "do we have a problem",
    "do we have a meeting tomorrow",
    "do we have school tomorrow",
    "do we have a guest room",
    "is there a doctor",
    "is there a problem",
    "got a second",
    "got a minute",
    "do we have wifi",
    "do we have power",
    "do we have hot water",
    "do we have internet",
    "do we have a spare key",
    "do we have a printer",
    "do we have a car",
    "is there anyone home",
    "do we have visitors",
    "do we have company",
    "do we have any news",
    "do we have any plans",
    "do we have a thermostat",
]

#:...and the groceries that must still land, so the exclude cannot be widened
#: into uselessness.
BARE_STILL_GROCERIES = [
    ("do we have any butter", "butter"),
    ("do we have olive oil", "olive oil"),
    ("do we have any pasta left", "pasta"),
    ("do we have coffee", "coffee"),
    ("is there any bread", "bread"),
    ("got any cheese", "cheese"),
    ("do we have eggs in the fridge", "eggs"),
    ("are we out of coffee", "coffee"),
]


@pytest.mark.parametrize("text", BARE_NOT_GROCERIES)
def test_the_bare_rule_declines_a_non_grocery_noun(text):
    assert _capread(text)[0] != "app_list_pantry", text


@pytest.mark.parametrize("text,item", BARE_STILL_GROCERIES)
def test_the_bare_rule_still_answers_a_grocery(text, item):
    assert _capread(text) == ("app_list_pantry", item), text
