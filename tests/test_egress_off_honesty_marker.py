"""The egress-off honesty marker must be phrasing-proof, not
surface-bound.

With the web doors shut, one phrasing gets the marker and a rephrasing does
not:

    "what can you do"
      -> "... a calculator, health checks, trivia, and web tools
          (off - Settings > Egress)."

    "what web tools do you have"
      -> "I've got a mix of classroom-friendly Web 2.0 tools from the 2008
          book summary - blogs, wikis, podcasts..."
         sources: wikisummaries_en_all_maxi_2021_04

with ``web_search`` and ``web_fetch`` both ``open=false`` in /system/status. A
user asking the direct question is told PuffPAI HAS web tools and never told
they are switched off: the exact overstatement the marker exists to prevent,
reachable by rephrasing.

This file pins four direct phrasings, in both door states.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402
from puffpai import capability_registry as capreg  # noqa: E402

#: The direct question, plus three rephrasings of it.
PHRASINGS = [
    "what web tools do you have",
    "what internet tools do you have",
    "can you search the web",
    "can you go online",
]

#: The ZIM answer a fall-through produces. Nothing in this path may produce it.
ZIM_LEAK = "blogs, wikis, podcasts"


@pytest.fixture
def doors(monkeypatch):
    """Control both web doors; returns a setter."""
    state = {"web_search": False, "web_fetch": False}

    def _allowed(door_id, *a, **k):
        return bool(state.get(door_id, True)), ""

    import puffpai.security.egress_doors as ed
    monkeypatch.setattr(ed, "egress_allowed", _allowed)
    return state


# ── the registry read itself ────────────────────────────────────────────────

def test_both_doors_shut_says_so_and_names_where_to_change_it(doors):
    said = capreg.describe_web_tools()
    assert "off" in said.lower()
    assert "Settings > Egress" in said
    assert ZIM_LEAK not in said


def test_both_doors_shut_never_claims_it_can_reach_the_internet(doors):
    said = capreg.describe_web_tools().lower()
    # No sentence may present the tools as available.
    assert "switched on" not in said
    assert "i can search the web" not in said


def test_one_door_open_is_reported_accurately(doors):
    doors["web_search"] = True
    said = capreg.describe_web_tools()
    assert "web search" in said
    assert "switched on" in said
    assert "page fetch is off" in said


def test_both_doors_open_claims_no_shut_door(doors):
    doors["web_search"] = True
    doors["web_fetch"] = True
    said = capreg.describe_web_tools()
    assert "switched on" in said
    assert "off" not in said.lower()


def test_an_unreadable_door_registry_does_not_invent_an_off(monkeypatch):
    """Marking a working tool "off" would be its own wrong answer."""
    import puffpai.security.egress_doors as ed

    def _boom(*a, **k):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(ed, "egress_allowed", _boom)
    said = capreg.describe_web_tools()
    assert "switched on" in said


# ── dispatch: every phrasing reaches that read ──────────────────────────────

@pytest.mark.parametrize("phrasing", PHRASINGS)
def test_each_phrasing_reaches_the_door_aware_read(phrasing, doors):
    assert capreg.describe_for(phrasing) == capreg.describe_web_tools()


@pytest.mark.parametrize("phrasing", PHRASINGS)
def test_each_phrasing_is_answered_before_rag(phrasing, doors):
    """``_try_capabilities`` runs inside the instant-answer layer, ahead of
    the router, the team and RAG - so a ZIM hit can never claim the turn."""
    said = api._try_capabilities(phrasing)
    assert said is not None, phrasing
    assert "Settings > Egress" in said
    assert ZIM_LEAK not in said


@pytest.mark.parametrize("phrasing", PHRASINGS)
def test_each_phrasing_says_the_doors_are_shut(phrasing, doors):
    assert "off" in api._try_capabilities(phrasing).lower()


# ── what must NOT be hijacked ───────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    # A TASK, not a question about the capability. It belongs to the tool that
    # would do it, which refuses honestly on its own when its door is shut.
    "can you search the web for the price of eggs",
    "search the web for tomorrow's tide table",
    "look up the boeing 747 wingspan",
    # The knowledge question this layer must never claim.
    "what do you know about web 2.0 tools in the classroom",
])
def test_a_web_task_is_left_to_its_own_path(text):
    assert api._WEB_TOOLS_ASK_RE.search(text) is None


def test_the_overview_phrasing_still_answers_the_overview(doors):
    """"what can you do" keeps answering with the overview."""
    said = api._try_capabilities("what can you do")
    assert said is not None
    assert "I'm one system" in said
    assert "web tools (off" in said
