"""Two FreshTable answers that must not be confidently wrong.

Shopping list: "what do I need from the store" must not fall past the literal
words "shopping"/"grocery list" into RAG, which invents a plausible grocery
list. The store/buy/need phrasings are declared on the shopping-list reader,
so the check here is the ROUTING one: which tool the utterance selects.

Tonight's dinner: the bare word "dinner?" must not reach the agent, which
answers with the first entry in the plan (Monday) and labels it "tonight" on a
Saturday. The bare phrasings route to the meal reader, which reads the box's
local date. Both the day resolution and the honest empty answer are pinned.

The decline corpus at the bottom is the other half of the contract: phrasings
that must NOT be claimed by either reader.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from tests.modpatch import patched_modules

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.agents import household_reads  # noqa: E402

_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)


@pytest.fixture(scope="module")
def api_mod():
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules({"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        import importlib
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")
        yield mod


def _capread_tool(mod, text):
    """The tool ``_try_capability_read`` would dispatch, without invoking it."""
    for tool_name, match_re, exclude_re, _needs_query in mod._CAPREAD_RULES:
        if not match_re.search(text):
            continue
        if exclude_re is not None and exclude_re.search(text):
            continue
        return tool_name
    return None


# ── ────────────────────────────────────────────────────────────────────

class TestShoppingListPhrasings:
    @pytest.mark.parametrize("text", [
        "what do I need from the store",          # the primary phrasing
        "What do I need from the store?",
        "what do we need from the store",
        "what should I buy",
        "what should we buy",
        "what do i need to buy",
        "what do we need at the market",
        "whats on the shopping list",             # the control that already worked
        "grocery list",
    ])
    def test_it_reaches_the_real_list(self, api_mod, text):
        assert _capread_tool(api_mod, text) == "app_list_shopping"

    @pytest.mark.parametrize("text", [
        "order my groceries",                     # a command with its own tool
        "put in my grocery order",
        "add milk to the shopping list",          # a write
        "what supplies do I need",                # the prep readers own this
        "what emergency supplies should I buy",
        "what do I need for my workout",
    ])
    def test_it_does_not_steal_other_questions(self, api_mod, text):
        assert _capread_tool(api_mod, text) != "app_list_shopping"


# ── tonight's dinner ───────────────────────────────────────────────────

_SATURDAY = datetime(2026, 9, 5, 18, 30)   # a Saturday evening


class _FakeApp:
    def __init__(self, plan):
        self._plan = plan

    def fresh_get_meal_plan(self):
        return list(self._plan)


def _week():
    """A plan whose FIRST entry is Monday — the entry the wrong answer used."""
    return [{"day": day, "meal": "dinner", "dish": f"{day.capitalize()} dish"}
            for day in ("monday", "tuesday", "wednesday", "thursday",
                        "friday", "saturday", "sunday")]


class TestBareDinner:
    @pytest.mark.parametrize("text", [
        "dinner?",                                # the primary phrasing
        "dinner",
        "Dinner?",
        "dinner tonight?",
        "what's dinner",
        "whats for dinner tonight",               # the control that already worked
        "what is for dinner tonight",
    ])
    def test_it_reaches_the_meal_reader(self, text):
        assert household_reads.match(text) == "meal_today"

    def test_it_answers_with_the_boxs_local_day(self):
        answer = household_reads.meal_today(app=_FakeApp(_week()), now=_SATURDAY)
        assert "Saturday" in answer
        assert "Saturday dish" in answer
        assert "Monday" not in answer

    def test_the_first_entry_is_never_mistaken_for_tonight(self):
        """Monday's dish must not be returned on a Saturday."""
        answer = household_reads.meal_today(app=_FakeApp(_week()), now=_SATURDAY)
        assert "Monday dish" not in answer

    def test_an_empty_slot_says_so_instead_of_guessing(self):
        plan = [e for e in _week() if e["day"] != "saturday"]
        answer = household_reads.meal_today(app=_FakeApp(plan), now=_SATURDAY)
        assert "Saturday" in answer
        assert "Nothing recorded yet" in answer
        for day in ("Monday dish", "Sunday dish"):
            assert day not in answer

    def test_an_empty_plan_says_so(self):
        answer = household_reads.meal_today(app=_FakeApp([]), now=_SATURDAY)
        assert "Nothing recorded yet" in answer

    @pytest.mark.parametrize("weekday,expected", [
        (datetime(2026, 9, 7), "Monday"),
        (datetime(2026, 9, 9), "Wednesday"),
        (datetime(2026, 9, 13), "Sunday"),
    ])
    def test_every_day_resolves_to_itself(self, weekday, expected):
        answer = household_reads.meal_today(app=_FakeApp(_week()), now=weekday)
        assert f"{expected} dish" in answer


# ── the decline corpus ───────────────────────────────────────────────────────

class TestDeclineCorpus:
    @pytest.mark.parametrize("text", [
        "plan meals for the week",
        "plan dinner for the week",
        "set dinner to tacos on friday",
        "add dinner to the calendar",
        "change dinner on wednesday",
        "give me a recipe for dinner",
        "order my groceries",
        "what time is dinner at the restaurant on saturday for eight people "
        "because I need to know before I book the table",
    ])
    def test_the_meal_reader_declines(self, text):
        assert household_reads.match(text) != "meal_today"


class TestTheTwoLayersDoNotCollide:
    """``_try_capability_read`` runs BEFORE the household reader, so a rule in
    that table claiming "dinner?" would silently break the meal reader."""

    @pytest.mark.parametrize("text", ["dinner?", "dinner", "what's dinner"])
    def test_no_capability_rule_claims_the_bare_dinner(self, api_mod, text):
        assert _capread_tool(api_mod, text) is None

    @pytest.mark.parametrize("text", [
        "what do I need from the store", "what should I buy"])
    def test_the_meal_reader_leaves_the_store_questions_alone(self, text):
        assert household_reads.match(text) != "meal_today"

    def test_the_meal_plan_command_fastpath_still_owns_its_phrasing(self, api_mod):
        assert api_mod._MEAL_PLAN_RE.search("plan meals for the week")
        assert not api_mod._MEAL_PLAN_RE.search("dinner?")
