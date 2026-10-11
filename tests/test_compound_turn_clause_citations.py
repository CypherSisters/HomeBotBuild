"""A compound turn's citations must say which clause they back.

The failure mode::

    POST /chat/send {"message": "tell me about the Great Wall of China,
                                 then check if any lights are on"}
      sources[] for the whole turn = 8, and every one is topically wrong:
        "Silenced: China's Great Wall of Censorship"
        "Setup a private VPN for family in China?"
        "Check engine light"
        "Isuzu Trooper Check Engine Light Trouble Code Reading"

while the standalone control ("when was the Great Wall of China built")
returns 5/5 hits titled "Great Wall of China".

Retrieval is per clause: the journal for such a turn shows two separate
``Route: rag`` lines, one per clause, then ``Compound turn: 2 clauses executed
in order``. The "Check engine light" records are the SECOND clause's own hits.
Pooling both clauses' hits into one anonymous ``sources[]`` makes the
provenance confidently wrong: the Great Wall prose appears to be cited by an
Isuzu Trooper trouble-code article.

This file pins the contract: each clause retrieves with only its own text, its
hits carry the clause that retrieved them, and the merge keeps order and drops
repeats (``_merge_clause_sources``).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402

CLAUSE_A = "tell me about the Great Wall of China"
CLAUSE_B = "check if any lights are on"
COMPOUND = f"{CLAUSE_A}, then {CLAUSE_B}"

#: The titles from the pooled sources[] list, split by the clause
#: whose keywords actually retrieved them.
WALL_HITS = [
    {"title": "Great Wall of China", "source_type": "zim_fts"},
    {"title": "Silenced: China's Great Wall of Censorship",
     "source_type": "zim_fts"},
]
LIGHT_HITS = [
    {"title": "Check engine light", "source_type": "zim_fts"},
    {"title": "Isuzu Trooper Check Engine Light Trouble Code Reading",
     "source_type": "zim_fts"},
]

#: A keyword that must never appear in the OTHER clause's citations.
B_KEYWORD = "engine light"
A_KEYWORD = "great wall"


# ── the merge function's own contract ────────────────────────────────────────

def test_merge_keeps_order_and_drops_repeats():
    shared = {"title": "Shared doc", "source_type": "user_upload"}
    merged = api._merge_clause_sources([
        (0, CLAUSE_A, WALL_HITS[0]),
        (0, CLAUSE_A, shared),
        (1, CLAUSE_B, dict(shared)),
        (1, CLAUSE_B, LIGHT_HITS[0]),
    ])
    assert [s["title"] for s in merged] == [
        "Great Wall of China", "Shared doc", "Check engine light"]
    # A record cited by both clauses keeps the FIRST clause that cited it.
    assert merged[1]["clause"] == CLAUSE_A


def test_merge_tags_every_record_with_its_clause():
    merged = api._merge_clause_sources(
        [(0, CLAUSE_A, s) for s in WALL_HITS]
        + [(1, CLAUSE_B, s) for s in LIGHT_HITS])
    by_clause = {}
    for source in merged:
        by_clause.setdefault(source["clause"], []).append(source["title"])
    assert set(by_clause) == {CLAUSE_A, CLAUSE_B}
    assert all(s["clause_index"] in (0, 1) for s in merged)


def test_merge_never_mutates_the_retrievers_own_record():
    original = dict(WALL_HITS[0])
    api._merge_clause_sources([(0, CLAUSE_A, WALL_HITS[0])])
    assert WALL_HITS[0] == original


def test_merge_survives_a_bare_source_and_an_unhashable_one():
    assert api._merge_clause_sources(None) == []
    assert api._merge_clause_sources([{"title": "bare"}]) == [{"title": "bare"}]
    weird = {"nested": {"set": object()}}
    assert len(api._merge_clause_sources([(0, CLAUSE_A, weird)])) == 1


# ── end to end through the chat pipeline ─────────────────────────────────────

def test_each_clauses_sources_carry_no_other_clauses_hits(monkeypatch):
    """The compound turn: clause A's citations contain no clause-B keyword."""
    seen = []

    # **kw so the double tracks the real _process_chat_query signature, which
    # takes `conversation_id`.
    def fake_process(message, history=None, **kw):
        seen.append(message)
        if "great wall" in message.lower():
            return {"response": "The Great Wall is a fortification.",
                    "sources": list(WALL_HITS), "suggestions": []}
        return {"response": "Kitchen Light is on.",
                "sources": list(LIGHT_HITS), "suggestions": []}

    def fake_compound(user_message, read_handler, requester="owner"):
        return "\n\n".join(read_handler(c) for c in (CLAUSE_A, CLAUSE_B))

    monkeypatch.setattr(api, "_try_compound_request", fake_compound)
    monkeypatch.setattr(api, "_augment_persona_with_memory",
                        lambda persona, uid: persona)
    monkeypatch.setattr(api, "_load_persona", lambda: "p")
    monkeypatch.setattr(api, "_remember_for_user", lambda *a: None)
    monkeypatch.setattr(api, "_requester_for", lambda uid: "owner")
    monkeypatch.setattr(api, "_detect_route", lambda *a, **k: "rag")

    real = api._process_chat_query

    def dispatch(message, history=None, **kw):
        if message == COMPOUND:
            return real(message, history, **kw)
        return fake_process(message, history, **kw)

    monkeypatch.setattr(api, "_process_chat_query", dispatch)
    result = dispatch(COMPOUND)

    # Retrieval saw ONLY each clause's own text — never the whole turn.
    assert seen == [CLAUSE_A, CLAUSE_B]

    a_sources = [s for s in result["sources"] if s["clause"] == CLAUSE_A]
    b_sources = [s for s in result["sources"] if s["clause"] == CLAUSE_B]
    assert a_sources and b_sources
    assert not any(B_KEYWORD in s["title"].lower() for s in a_sources), (
        "clause A's citations picked up clause B's keyword hits")
    assert not any(A_KEYWORD in s["title"].lower() for s in b_sources)
    # Order is the order the clauses were asked.
    assert [s["clause_index"] for s in result["sources"]] == [0, 0, 1, 1]
