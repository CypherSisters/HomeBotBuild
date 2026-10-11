"""An answer out of a made-up world must say so.

Without framing:

    "what is the capital of Wakanda"
      -> "Birnin Zana... the Golden City" 2 zim_fts sources ("Wakanda")
         flat factual answer, no fictional-context framing

    "how many dragons currently live in Westeros"
      -> "just one... Drogon" 2 zim_fts sources ("A Dance with Dragons")
         flat factual answer, no fictional-context framing

Both are grounded and cited, since the library holds real articles ABOUT
fiction, so the answer is framing, not refusal. The signal comes from the retrieved rows
themselves (title, lead sentence), never from a model.

The lead sentences below are the real shape Wikipedia-derived ZIM articles
carry ("X is a fictional country... published by Marvel Comics").
"""

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import fiction_context as fc  # noqa: E402

_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

WAKANDA_HITS = [
    {"title": "Wakanda", "library": "wikipedia_en_all",
     "text": "Wakanda is a fictional country in East Africa in American "
             "comic books published by Marvel Comics. Its capital is Birnin "
             "Zana, also known as the Golden City."},
    {"title": "Black Panther (character)", "library": "wikipedia_en_all",
     "text": "Black Panther is a superhero appearing in American comic books "
             "published by Marvel Comics."},
]

WESTEROS_HITS = [
    {"title": "A Dance with Dragons", "library": "wikipedia_en_all",
     "text": "A Dance with Dragons is the fifth novel in A Song of Ice and "
             "Fire, the epic fantasy series by George R. R. Martin."},
]

#: A fictional place with no universe in the map — the generic clause.
GENERIC_HITS = [
    {"title": "Lilliput", "library": "wikipedia_en_all",
     "text": "Lilliput and Blefuscu are two fictional island nations that "
             "appear in Jonathan Swift's 1726 novel Gulliver's Travels."},
]

REAL_HITS = [
    {"title": "Great Wall of China", "library": "wikipedia_en_all",
     "text": "The Great Wall of China is a series of fortifications built "
             "across the historical northern borders of ancient Chinese "
             "states."},
    {"title": "Mariana Trench", "library": "wikipedia_en_all",
     "text": "The Mariana Trench is an oceanic trench in the western Pacific "
             "Ocean, the deepest part of the world's oceans."},
]


# ── detection ───────────────────────────────────────────────────────────────

def test_wakanda_rows_name_the_marvel_universe():
    assert fc.detect_framing(WAKANDA_HITS) == "In the Marvel universe"


def test_westeros_rows_name_martins_world():
    assert fc.detect_framing(WESTEROS_HITS) == "In George R. R. Martin's Westeros"


def test_a_fictional_lead_with_no_known_universe_gets_the_generic_clause():
    assert fc.detect_framing(GENERIC_HITS) == "In fiction"


@pytest.mark.parametrize("hits", [REAL_HITS, [], None])
def test_real_world_rows_are_never_framed(hits):
    assert fc.detect_framing(hits) is None


def test_rows_below_the_window_are_not_consulted():
    """Documented limit: only the top rows supply the signal."""
    padded = REAL_HITS + REAL_HITS + WAKANDA_HITS
    assert fc.detect_framing(padded) is None


# ── framing ─────────────────────────────────────────────────────────────────

def test_the_wakanda_answer_is_framed_in_one_clause():
    reply = fc.frame_for_hits(
        "Birnin Zana, also called the Golden City, is the capital.",
        WAKANDA_HITS)
    assert reply.startswith("In the Marvel universe, Birnin Zana")


def test_the_dragon_answer_is_framed():
    reply = fc.frame_for_hits(
        "There is just one dragon left: Drogon.", WESTEROS_HITS)
    assert reply.startswith("In George R. R. Martin's Westeros, there is just")


def test_a_real_answer_is_returned_byte_identical():
    answer = "The Great Wall of China was built over many centuries."
    assert fc.frame_for_hits(answer, REAL_HITS) == answer


def test_framing_is_not_applied_twice():
    already = ("In the Marvel universe, Wakanda is a fictional nation.")
    assert fc.frame_for_hits(already, WAKANDA_HITS) == already


def test_an_answer_that_already_says_fictional_is_left_alone():
    honest = "Wakanda is a fictional country, so it has no real capital."
    assert fc.frame_for_hits(honest, WAKANDA_HITS) == honest


def test_framing_never_raises_on_junk_rows():
    assert fc.frame_for_hits("answer", ["not a dict", None, 7]) == "answer"


def test_empty_reply_is_untouched():
    assert fc.frame_for_hits("", WAKANDA_HITS) == ""


# ── config flag: fiction_framing default OFF ────────────────────────────────
#
# The heuristic above also fires on real-world subjects it has no business
# tagging as fiction ("black panther" -> Marvel, "star wars program" SDI ->
# Star Wars universe, "gotham new york nickname" -> DC, "vulcan planet
# volcano" -> Star Trek). It ships disabled by default behind
# ``chromadb.fiction_framing`` in puffpai_config.json; the two call sites in
# api_server.py (_query_ollama_direct) only invoke detect_framing/frame_reply
# when the flag is explicitly true.

@pytest.fixture()
def api_mod():
    """Import api_server lazily with heavy deps mocked, like test_api_server.py."""
    from tests.modpatch import patched_modules

    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules({"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")
        yield mod


def _stub_rag(mod, monkeypatch, hits):
    """Force _query_ollama_direct through the RAG path with fixed hits."""
    monkeypatch.setattr(mod, "_should_skip_rag", lambda text: False)
    monkeypatch.setattr(
        mod, "_get_rag_context",
        lambda question, n_results=12: {
            "context_text": "stub context",
            "sources": [],
            "hits": hits,
        },
    )
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"message": {"content": "Birnin Zana is the capital."}}
    monkeypatch.setattr(mod, "_ollama_chat", lambda *a, **kw: resp)


def test_default_config_leaves_the_reply_unframed(api_mod, monkeypatch):
    """Flag absent/False (the shipped default) -> no framing, even though
    detect_framing would otherwise fire on these rows."""
    mod = api_mod
    _stub_rag(mod, monkeypatch, WAKANDA_HITS)
    assert mod._cfg.chromadb.fiction_framing is False

    result = mod._query_ollama_direct("what is the capital of Wakanda", [], None)

    assert result == "Birnin Zana is the capital."
    assert not result.startswith("In the Marvel universe")


def test_flag_true_restores_old_framing_behavior(api_mod, monkeypatch):
    """Flag explicitly True (old default) -> framing occurs."""
    mod = api_mod
    _stub_rag(mod, monkeypatch, WAKANDA_HITS)
    monkeypatch.setattr(mod._cfg.chromadb, "fiction_framing", True)

    result = mod._query_ollama_direct("what is the capital of Wakanda", [], None)

    assert result.startswith("In the Marvel universe, Birnin Zana")


def test_flag_true_with_non_str_reply_passes_through_unchanged(api_mod, monkeypatch):
    """Flag True + a non-str reply through the frame_reply codepath must not
    raise (frame_reply raises AttributeError on a non-str reply) and must
    return the original reply value unchanged."""
    mod = api_mod
    _stub_rag(mod, monkeypatch, WAKANDA_HITS)
    monkeypatch.setattr(mod._cfg.chromadb, "fiction_framing", True)

    non_str_reply = ["not", "a", "string"]
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"message": {"content": "placeholder"}}
    monkeypatch.setattr(mod, "_ollama_chat", lambda *a, **kw: resp)

    # frame_reply is called on `raw`, which comes straight from the Ollama
    # response content; patch fiction_context.frame_reply's first argument
    # by making the parsed content itself the non-str object.
    resp.json.return_value = {"message": {"content": non_str_reply}}

    result = mod._query_ollama_direct("what is the capital of Wakanda", [], None)

    assert result == non_str_reply
