"""The common phrasings for "look this up" must bind the web-search tool.

Assessment gap #8: the ``utility.websearch`` capability triggered on "search the
web / look up / google / search for / search online", but not on the everyday
verbs a person actually reaches for — "research", "find out", "online", "on the
web". Those phrasings would fall through to no tool, so a request the planner
could satisfy with a search never even bound the tool. This pins the widened
trigger set so a future edit that drops one fails here rather than in the field.

Routing is substring matching (``capability_registry._cap_matches``): a trigger
that is a substring of the lowered query binds the capability's tools. No
network, no config — the registry is a pure data structure.

Run: pytest tests/test_websearch_triggers.py -q
"""

from __future__ import annotations

import pytest

from puffpai import capability_registry as reg

# The gap-#8 phrasings. Each must bind the web-search tool; none is in the
# pre-existing trigger set (that was the gap).
BINDING_QUERIES = [
    "can you research that topic for me",
    "find out when the library opens",
    "what's the news online today",
    "look that up on the web",
    # And the pre-existing ones must still bind — the widening is additive.
    "search the web for train times",
    "look it up online",
    "google the recipe",
]

# Everyday requests that name no search intent must NOT bind web_search — the
# widened keywords must not become a catch-all that fires on unrelated turns.
NON_BINDING_QUERIES = [
    "tell me a joke",
    "turn on the kitchen light",
    "what's on my calendar",
    "play some music",
]


@pytest.mark.parametrize("query", BINDING_QUERIES)
def test_search_phrasings_bind_the_web_search_tool(query):
    assert "web_search" in reg.tools_for_query(query), query


@pytest.mark.parametrize("query", NON_BINDING_QUERIES)
def test_unrelated_turns_do_not_bind_web_search(query):
    assert "web_search" not in reg.tools_for_query(query), query


def test_gap8_triggers_are_present_on_the_capability():
    """The four gap-#8 triggers are declared on utility.websearch itself — a
    direct guard against a refactor silently dropping them from the data."""
    cap = reg.get("utility.websearch")
    assert cap is not None
    triggers = set(cap.keywords) | set(cap.phrases)
    for needed in ("research", "find out", "online", "on the web"):
        assert needed in triggers, needed


def test_websearch_still_owned_by_toolsmith_and_egress_gated():
    """The trigger widening must not disturb ownership or the egress posture the
    planner cross-check and the dispatch gate both depend on.

    The status assertion CHANGED, deliberately. This
    test used to pin ``status == NEEDS_AUTH``, which pinned a defect: the
    declaration was static, so "what can you do" told the owner web search
    "needs a one-time setup first" on a box where both doors were open and the
    chat fastpath was answering live searches in 0.75 s. She believed it.

    What actually needs pinning is the EGRESS POSTURE, and that never lived in
    the ``status`` field — it lives in the doors and the chat opt-in, both off
    by default, both asserted below. The status is now measured at read time by
    ``capability_probes``; see ``tests/test_websearch_availability.py``.
    """
    cap = reg.get("utility.websearch")
    assert cap.agent == "toolsmith"
    # Declared live, but PROBED — never a bare static claim again.
    assert cap.status == reg.LIVE
    from puffpai import capability_probes
    assert "utility.websearch" in capability_probes._PROBES
    # The posture this test is really about: gated, and shut by default.
    from puffpai.security import egress_policy
    assert "web_search" in egress_policy.CHAT_EGRESS_DOORS
    assert egress_policy.policy("chat_allow_egress").default is False
