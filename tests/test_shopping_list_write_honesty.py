"""A shopping-list write is confirmed by the store, or it is not confirmed.

The failure to prevent (gpt-oss:20b)::

    "add SAMPLE oat milk to the shopping list, then tell me how many items
     are on the list"
    -> "Added sample oat milk to the shopping list. There are five items
        listed."

while FreshTable's ``shoppingList`` never sees the item and "five" is
invented. Two causes, one class: prose that DESCRIBES an action instead of
REPORTING one.

* A write to Home Assistant's ``shopping_list`` integration lands where
  nothing in this tree reads, while the app, the tile and every other writer
  use FreshTable (``agents/shopping_writes.py`` is the one writer).
* A confirmation from a template keyed on the INTENT (``concierge._render``)
  reads the same whether anything was written or not, so the compound path
  must not repeat it without a read-back (``_COMPOUND_VERIFIABLE_INTENTS``
  covers device control only; ``list_add`` is store-verified separately).

Every fixture item carries the ``SAMPLE `` prefix: no real household data.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402
from puffpai.agents import household_reads, shopping_writes  # noqa: E402
from puffpai.agents.concierge import ConciergeAgent  # noqa: E402
from puffpai.agents.steward import StewardAgent  # noqa: E402

ITEM = "SAMPLE oat milk"
ADD = f"add {ITEM} to the shopping list"
COUNT = "tell me how many items are on the list"
COMPOUND = f"{ADD}, then {COUNT}"


class FakeApp:
    """The FreshTable rows, in memory. Mirrors AppData's two shopping calls."""

    def __init__(self, rows=None, fail=False):
        self.rows = list(rows or [])
        self.fail = fail
        self.writes = 0

    def fresh_add_shopping(self, name, quantity=1, est_price=0, category=None):
        self.writes += 1
        if self.fail:
            raise RuntimeError("freshtable unavailable")
        self.rows.append({"id": f"id_{len(self.rows)}", "name": name,
                          "quantity": quantity, "checked": False})
        return f"Added {name} to the shopping list, quantity {quantity}."

    def fresh_shopping_items(self):
        return [r for r in self.rows if not r.get("checked")]


class SilentApp(FakeApp):
    """The write returns cleanly and the row never appears."""

    def fresh_add_shopping(self, name, quantity=1, est_price=0, category=None):
        self.writes += 1
        return "Added."


#: Digit-free on purpose: the "every number in the reply is the store count"
#: assertions must not be satisfied by a number that came from an item name.
SEED = ["SAMPLE alpha", "SAMPLE bravo", "SAMPLE charlie"]


def seeded(**kw):
    return FakeApp([{"id": f"id_{n}", "name": n, "checked": False}
                    for n in SEED], **kw)


@pytest.fixture
def app(monkeypatch):
    """One in-memory store behind every seam that reaches FreshTable."""
    store = seeded()
    monkeypatch.setattr(shopping_writes, "_store",
                        lambda app=None: app if app is not None else store)
    monkeypatch.setattr(household_reads, "_app_data",
                        lambda app=None: app if app is not None else store)
    monkeypatch.setattr(api, "_app_installed", lambda app_id: True)
    return store


def numbers(text):
    return set(re.findall(r"\d+", text or ""))


# ── the writer: evidence, never a claim ──────────────────────────────────────

def test_a_verified_write_lands_in_freshtable_and_counts_the_store(app):
    result = shopping_writes.add_verified(ITEM)
    assert result["store"] == "freshtable"
    assert result["written"] is True and result["verified"] is True
    assert result["count"] == 4 == len(app.fresh_shopping_items())
    assert [r["name"] for r in app.rows][-1] == ITEM


def test_a_raising_store_is_reported_as_a_failure_not_an_add(app):
    app.fail = True
    result = shopping_writes.add_verified(ITEM)
    assert result["written"] is False and result["verified"] is False
    assert result["error"]
    assert "Added" not in shopping_writes.reply_for(result)
    assert len(app.rows) == 3


def test_a_silent_write_that_never_lands_is_caught_by_the_read_back(monkeypatch):
    """The call returns, the list is unchanged."""
    store = SilentApp([{"id": "id_0", "name": "SAMPLE alpha"}])
    monkeypatch.setattr(shopping_writes, "_store", lambda app=None: store)
    result = shopping_writes.add_verified(ITEM)
    assert result["written"] is True and result["verified"] is False
    assert "read-back" in (result["error"] or "")
    assert "Added" not in shopping_writes.reply_for(result)


def test_the_ha_shopping_list_is_a_mirror_only_and_only_after_verification(app):
    calls = []

    class Bridge:
        def call_service(self, domain, service, **kw):
            calls.append((domain, service, kw))

    ok = shopping_writes.add_verified(ITEM, ha=Bridge())
    assert ok["mirrored"] is True
    assert calls == [("shopping_list", "add_item", {"name": ITEM})]

    app.fail = True
    calls.clear()
    failed = shopping_writes.add_verified("SAMPLE rice", ha=Bridge())
    assert failed["mirrored"] is False and calls == []


def test_a_failing_mirror_never_turns_a_real_add_into_an_apology(app):
    class Broken:
        def call_service(self, *a, **kw):
            raise RuntimeError("HA is down")

    result = shopping_writes.add_verified(ITEM, ha=Broken())
    assert result["verified"] is True and result["mirrored"] is False
    assert shopping_writes.reply_for(result).startswith(f"Added {ITEM}")


def test_the_steward_writes_the_canonical_store_even_with_no_house(app):
    """HA down must not block the list this appliance actually shows."""
    class NoHouse:
        def _house(self, envelope):
            raise RuntimeError("no HA token")

    result = StewardAgent._add_to_shopping(NoHouse(), None, ITEM)
    assert result["verified"] is True and result["mirrored"] is False
    assert [r["name"] for r in app.rows][-1] == ITEM


# ── the wording: composed from the evidence ──────────────────────────────────

def test_the_concierge_template_is_fed_by_the_verified_result(app):
    verified = shopping_writes.add_verified(ITEM)
    prose = ConciergeAgent._render(None, "list_add", None, verified)
    assert prose == f"Added {ITEM} to the shopping list. That's 4 items on it now."
    assert numbers(prose) == {"4"} == {str(len(app.fresh_shopping_items()))}


def test_an_unverified_result_never_renders_as_added(app):
    app.fail = True
    prose = ConciergeAgent._render(None, "list_add", None,
                                  shopping_writes.add_verified(ITEM))
    assert "Added" not in prose
    assert "the list is unchanged" in prose


def test_a_todo_list_add_keeps_its_own_wording(app):
    prose = ConciergeAgent._render(
        None, "list_add", "todo.chores",
        {"ok": True, "list": "todo", "item": "SAMPLE water plants"})
    assert prose == "Added SAMPLE water plants to the todo list."


# ── the single-clause fastpath ───────────────────────────────────────────────

def test_the_single_clause_add_is_verified_the_same_way(app):
    reply = api._try_shopping_add(ADD)
    assert reply == f"Added {ITEM} to the shopping list. That's 4 items on it now."
    assert app.writes == 1 and len(app.rows) == 4


def test_the_single_clause_add_admits_a_failed_write(app):
    app.fail = True
    reply = api._try_shopping_add(ADD)
    assert "Added" not in reply and "unchanged" in reply
    assert len(app.rows) == 3


@pytest.mark.parametrize("phrase", [
    "how many items are on the shopping list",
    "what's on the shopping list",
    "add a reminder to call the vet",
    "remove SAMPLE oat milk from the shopping list",
    "order my groceries",
])
def test_the_fastpath_declines_everything_that_is_not_a_one_item_add(app, phrase):
    assert api._try_shopping_add(phrase) is None


# ── the count: read from the store, never from a model ───────────────────────

def test_the_count_question_is_answered_by_a_deterministic_reader(app):
    assert household_reads.match("how many items are on the shopping list") \
        == "shopping_status"
    # The capability-read table hands the counting phrasings down on purpose.
    assert api._try_capability_read("how many items are on the shopping list") \
        is None
    reply = api._try_household_read("how many items are on the shopping list")
    assert reply and numbers(reply) == {"3"}
    assert "3 items" in reply


def test_the_count_reader_reads_the_store_after_a_write(app):
    shopping_writes.add_verified(ITEM)
    reply = api._try_household_read("how many items are on the shopping list")
    assert numbers(reply) == {"4"}
    # The count does not recite the list. The number is still read from the
    # store after the write (that is what this test is for), and the CONTENTS
    # phrasing still names every item.
    assert ITEM not in reply
    assert ITEM in household_reads.shopping_status()


def test_the_bare_list_clause_is_resolved_to_the_list_the_turn_named():
    assert api._resolve_bare_list(COUNT, "shopping") == \
        "tell me how many items are on the shopping list"
    assert api._resolve_bare_list(COUNT, "todo") == \
        "tell me how many items are on the todo list"
    # No earlier list_add in the turn: the words are left exactly as asked.
    assert api._resolve_bare_list(COUNT, None) == COUNT
    # A clause that names its own list is never rewritten.
    named = "whats on the shopping list"
    assert api._resolve_bare_list(named, "todo") == named


def test_an_empty_list_is_a_real_answer_not_a_shrug(monkeypatch):
    monkeypatch.setattr(household_reads, "_app_data",
                        lambda app=None: FakeApp([]))
    reply = household_reads.answer("how many items are on the shopping list")
    assert "empty" in reply and not numbers(reply)


# ── the compound turn, end to end ────────────────────────────────────────────

def _team_double(monkeypatch, app, *, bridge=None):
    """The team route, wired to the real writer and the real renderer."""
    monkeypatch.setattr(api, "_detect_route", lambda *a, **kw: "agent")
    monkeypatch.setattr(api, "_try_untargeted_house_command", lambda c: None)

    def fake_team(message, route, requester="owner", history=None):
        item = shopping_writes.match(message)
        if item is None:
            return None
        reply = shopping_writes.add_verified(item, ha=bridge)
        prose = ConciergeAgent._render(None, "list_add", None, reply)
        return {"response": prose, "sources": [], "suggestions": [],
                "team": {"agent": "steward", "intent": "list_add",
                         "targets": [], "result": reply}}

    monkeypatch.setattr(api, "_try_team_chat", fake_team)


def _read_handler(clause):
    """A clause reader with NO model behind it: the deterministic readers only.

    If the count clause ever stops being claimed by one of these, this returns
    None and the compound turn says so, which is the point. Sent to the LLM
    instead, the same clause comes back "There are five items listed."
    """
    return (api._try_capability_read(clause)
            or api._try_household_read(clause))


def test_the_compound_turn_writes_once_and_counts_what_it_wrote(monkeypatch, app):
    _team_double(monkeypatch, app)
    reply = api._try_compound_request(COMPOUND, _read_handler)

    assert app.writes == 1, "exactly one FreshTable row is written"
    assert len(app.rows) == 4
    assert [r["name"] for r in app.rows][-1] == ITEM
    assert f"Added {ITEM} to the shopping list" in reply
    # Every number in the reply is the store's own count. "five" cannot occur
    # because no number in this reply comes from prose.
    assert numbers(reply) == {"4"} == {str(len(app.fresh_shopping_items()))}
    assert "five" not in reply.lower()


def test_the_compound_turn_admits_a_write_that_did_not_land(monkeypatch, app):
    app.fail = True
    _team_double(monkeypatch, app)
    reply = api._try_compound_request(COMPOUND, _read_handler)

    assert len(app.rows) == 3
    assert "Added" not in reply
    assert "couldn't add" in reply and "unchanged" in reply
    # The count clause still answers from the store: 3, the unchanged truth.
    assert numbers(reply) == {"3"}


def test_a_confirmation_without_the_evidence_is_refused(app):
    """The compound gate itself: prose alone is not a receipt."""
    claimed = {"response": "Added SAMPLE oat milk to the shopping list.",
               "team": {"intent": "list_add",
                        "result": {"ok": True, "list": "shopping",
                                   "item": ITEM, "verified": False,
                                   "count": 3}}}
    out = api._store_verified_clause_reply(claimed["team"],
                                           claimed["response"])
    assert "Added" not in out and "unchanged" in out


def test_list_add_is_declared_a_store_verified_compound_intent():
    from puffpai.agents.steward import STATE_CHANGING_INTENTS
    assert "list_add" in api._COMPOUND_STORE_VERIFIED_INTENTS
    assert api._COMPOUND_STORE_VERIFIED_INTENTS <= STATE_CHANGING_INTENTS
    # The two verification mechanisms are disjoint: an HA entity read-back for
    # device control, a store read-back for an app write.
    assert not (api._COMPOUND_STORE_VERIFIED_INTENTS
                & api._COMPOUND_VERIFIABLE_INTENTS)
