"""A spoken remove from the shopping list, with a read-back.

"remove... from the shopping list" and "cross... off" need a deterministic
path of their own, the inverse of the ADD's: without one both fall through to
a READ and the whole list is recited back.

SAMPLE fixtures only — nothing here touches the household's real list.
"""

import pytest

from puffpai.agents import shopping_writes as sw


class FakeList:
    """A FreshTable shopping list with the real remove semantics."""

    def __init__(self, names):
        self.rows = [{"id": f"id_{n}", "name": name}
                     for n, name in enumerate(names)]

    def fresh_shopping_items(self):
        return list(self.rows)

    def fresh_remove_shopping(self, item_identifier):
        low = str(item_identifier).strip().lower()
        exact = [r for r in self.rows
                 if r["id"] == item_identifier or r["name"].lower() == low]
        if exact:
            victims = exact
        else:
            partial = [r for r in self.rows if low in r["name"].lower()]
            if len(partial) != 1:
                return [], [r["name"] for r in partial]
            victims = partial
        ids = {id(r) for r in victims}
        self.rows = [r for r in self.rows if id(r) not in ids]
        return [r["name"] for r in victims], []


def _list(*extra):
    return FakeList(["SAMPLE oat milk", "SAMPLE rice", *extra])


# ── the trigger table ────────────────────────────────────────────────────

@pytest.mark.parametrize("text,item", [
    ("remove SAMPLE oat milk from the shopping list", "SAMPLE oat milk"),
    ("Remove SAMPLE oat milk from my shopping list.", "SAMPLE oat milk"),
    ("take SAMPLE oat milk off the list", "SAMPLE oat milk"),
    ("take SAMPLE oat milk off the shopping list", "SAMPLE oat milk"),
    ("cross SAMPLE oat milk off the list", "SAMPLE oat milk"),
    ("cross SAMPLE oat milk off the grocery list", "SAMPLE oat milk"),
    ("delete SAMPLE oat milk from the shopping list", "SAMPLE oat milk"),
    ("scratch SAMPLE rice off the list", "SAMPLE rice"),
])
def test_every_phrasing_names_the_item(text, item):
    assert sw.match_remove(text) == item


@pytest.mark.parametrize("text", [
    "add SAMPLE oat milk to the shopping list",
    "what's on the shopping list",
    "how many items are on the list",
    "remove it from the list",
    "clear the shopping list",
    "remove the soccer reminder",
    "take the trash out",
])
def test_declines_everything_that_is_not_a_one_item_remove(text):
    assert sw.match_remove(text) is None


def test_the_add_rule_and_the_remove_rule_never_claim_the_same_turn():
    add = "add SAMPLE oat milk to the shopping list"
    remove = "remove SAMPLE oat milk from the shopping list"
    assert sw.match(add) and sw.match_remove(add) is None
    assert sw.match_remove(remove) and sw.match(remove) is None


# ── the write, and the read-back ─────────────────────────────────────────

def test_a_removal_is_confirmed_from_the_list_afterwards():
    store = _list("SAMPLE beans")
    reply = sw.handle_remove("remove SAMPLE oat milk from the shopping list",
                             app=store)
    assert reply == "Took SAMPLE oat milk off the shopping list. That's 2 items now."
    assert [r["name"] for r in store.rows] == ["SAMPLE rice", "SAMPLE beans"]


def test_the_last_item_reads_in_the_singular():
    store = FakeList(["SAMPLE oat milk", "SAMPLE rice"])
    reply = sw.handle_remove("cross SAMPLE oat milk off the list", app=store)
    assert "That's 1 item now." in reply


def test_an_item_that_was_never_there_is_told_plainly():
    store = _list()
    reply = sw.handle_remove("remove SAMPLE kale from the shopping list",
                             app=store)
    assert reply == "SAMPLE kale wasn't on the list."
    assert len(store.rows) == 2, "an honest miss may not change the list"


def test_an_ambiguous_name_removes_nothing_and_asks():
    store = FakeList(["SAMPLE milk chocolate", "SAMPLE milk bread"])
    reply = sw.handle_remove("take SAMPLE milk off the list", app=store)
    assert "more than one match" in reply
    assert "SAMPLE milk chocolate" in reply and "SAMPLE milk bread" in reply
    assert len(store.rows) == 2, "an ambiguous remove must take nothing"


def test_a_name_no_row_contains_is_simply_absent():
    """"SAMPLE milk" is not a substring of "SAMPLE oat milk" — it is a miss."""
    store = FakeList(["SAMPLE oat milk", "SAMPLE whole milk"])
    reply = sw.handle_remove("take SAMPLE milk off the list", app=store)
    assert reply == "SAMPLE milk wasn't on the list."
    assert len(store.rows) == 2


def test_an_exact_name_wins_over_a_substring():
    store = FakeList(["SAMPLE milk", "SAMPLE oat milk"])
    reply = sw.handle_remove("remove SAMPLE milk from the shopping list",
                             app=store)
    assert "Took SAMPLE milk off" in reply
    assert [r["name"] for r in store.rows] == ["SAMPLE oat milk"]


def test_a_broken_store_never_claims_a_removal():
    class Broken(FakeList):
        def fresh_remove_shopping(self, item_identifier):
            raise RuntimeError("disk full")

    reply = sw.handle_remove("remove SAMPLE oat milk from the shopping list",
                             app=Broken(["SAMPLE oat milk"]))
    assert "couldn't take" in reply and "unchanged" in reply


def test_an_unreadable_count_still_confirms_what_was_removed():
    class NoCount(FakeList):
        def fresh_shopping_items(self):
            raise RuntimeError("nope")

    reply = sw.handle_remove("remove SAMPLE oat milk from the shopping list",
                             app=NoCount(["SAMPLE oat milk"]))
    assert reply == "Took SAMPLE oat milk off the shopping list."


# ── the app_data half ────────────────────────────────────────────────────

def test_app_data_exposes_the_remover():
    from puffpai.app_data import AppData

    assert callable(getattr(AppData, "fresh_remove_shopping", None))


# ── the wiring ───────────────────────────────────────────────────────────

def test_it_is_registered_once_next_to_the_add():
    from tests.test_instant_answer_precedence import _call_order

    order = [name for _lineno, name in _call_order()]
    assert order.count("_try_shopping_remove") == 1
    assert order.index("_try_shopping_add") < order.index("_try_shopping_remove")
    assert order.index("_try_shopping_remove") < order.index("_try_capability_read")


def test_it_is_attributed_to_the_pantry_capability():
    from puffpai.chat_activity import FASTPATH_CAPABILITY, WRITE_FASTPATHS

    assert FASTPATH_CAPABILITY["_try_shopping_remove"] == "pantry.shopping"
    assert "_try_shopping_remove" in WRITE_FASTPATHS


# ── T8c: a remove shape ALWAYS answers from the store ────────────────────
#
# Live: "cross SAMPLE nothing off" named no list, so
# the shape matched nothing, the turn fell to RAG, and the library answered
# with an invented Wikispecies article about people named Cross.

@pytest.mark.parametrize("text,item", [
    ("cross SAMPLE nothing off", "SAMPLE nothing"),
    ("cross SAMPLE oat milk off", "SAMPLE oat milk"),
    ("scratch SAMPLE rice off", "SAMPLE rice"),
    ("strike unicorn tears off", "unicorn tears"),
])
def test_a_bare_cross_off_still_names_its_item(text, item):
    assert sw.match_remove(text) == item


@pytest.mark.parametrize("text", [
    "cross SAMPLE nothing off",
    "remove unicorn tears from the shopping list",
    "cross unicorn tears off the shopping list",
])
def test_an_item_that_was_never_there_is_answered_from_the_store(text):
    app = _list()
    reply = sw.handle_remove(text, app=app)
    assert reply is not None, "the turn must never fall through to RAG"
    assert reply.endswith("wasn't on the list.")
    assert len(app.rows) == 2, "nothing may be removed"


def test_the_nonsense_object_is_quoted_back_not_researched():
    app = _list()
    reply = sw.handle_remove("cross SAMPLE nothing off", app=app)
    assert reply == "SAMPLE nothing wasn't on the list."


def test_a_pronoun_object_is_asked_about_not_dropped():
    app = _list()
    reply = sw.handle_remove("cross it off the list", app=app)
    assert reply == "Which item should I take off the shopping list?"
    assert len(app.rows) == 2


def test_clearing_the_whole_list_is_declined_in_one_sentence():
    app = _list()
    reply = sw.handle_remove("take everything off the shopping list", app=app)
    assert "whole shopping list" in reply
    assert len(app.rows) == 2


@pytest.mark.parametrize("text", [
    "cross the street",
    "what's on the shopping list",
    "check the weather",
    "add SAMPLE rice to the shopping list",
])
def test_a_turn_that_is_not_a_remove_is_still_handed_on(text):
    assert sw.handle_remove(text, app=_list()) is None
