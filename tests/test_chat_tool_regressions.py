"""Regressions caught in the legacy chat tool layer (items 2.2, 2.4, 2.8).

Three defects in the legacy chat tool layer:

  2.2  "What's 847 times 62?" could not reach the calculator at all —
       _filter_tools_by_intent had no math branch, so the model saw only the
       knowledge tools; when it fell back to search_knowledge_base, THAT
       crashed (see 2.8) and the model did the math itself.
  2.4  list_indexed_libraries answered from indexed_libraries.json, which on
       a ZIM+FTS box only records ChromaDB ingestions (the user's uploads) —
       it listed two stray upload filenames instead of the 69 libraries that
       GET /knowledge/libraries reports.
  2.8  search_knowledge_base iterated a RAGResponse as if DualZoneRAG.query
       returned a list: "Error: 'RAGResponse' object is not iterable".
"""

import contextlib
import os
import pathlib
import sys
import types

import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from tests.modpatch import patched_modules  # noqa: E402


@contextlib.contextmanager
def _legacy_import_path():
    """Make src/puffpai importable flat, for create_tools' pre-package imports."""
    flat = str(pathlib.Path(__file__).resolve().parents[1] / "src" / "puffpai")
    sys.path.insert(0, flat)
    try:
        yield
    finally:
        try:
            sys.path.remove(flat)
        except ValueError:
            pass


def _build_tools(stand_in=None):
    """Real tool bodies bound to a minimal stand-in (no AssistantTools.__init__)."""
    from puffpai.assistant_tools import AssistantTools
    if stand_in is None:
        stand_in = type('_Stand', (), {'_ha_bridge': MagicMock()})()
    with _legacy_import_path():
        made = AssistantTools.create_tools(stand_in)
    return {t.name: t for t in made}, stand_in


# A stub api_server module WITHOUT the helpers, so tools under test exercise
# their standalone fallback paths deterministically even when another test
# file already imported the real puffpai.api_server into this process.
_NO_API = types.ModuleType('puffpai.api_server')


# ═══════════════════════════════════════════════════════════════════════════
#  2.2 — calculate: spoken math, and the tool actually being reachable
# ═══════════════════════════════════════════════════════════════════════════

class TestNormalizeMathExpression:
    CASES = [
        ("What's 847 times 62?", "847 * 62"),
        ("847 * 62", "847 * 62"),
        ("847 x 62", "847 * 62"),
        ("1,000 plus 2,500", "1000 + 2500"),
        ("100 divided by 4", "100 / 4"),
        ("2 to the power of 10", "2 ** 10"),
        ("7 mod 3", "7 % 3"),
    ]

    @pytest.mark.parametrize("raw,expected", CASES)
    def test_spoken_math_becomes_arithmetic(self, raw, expected):
        from puffpai.assistant_tools import normalize_math_expression
        assert normalize_math_expression(raw) == expected

    def test_percent_of(self):
        from puffpai.assistant_tools import normalize_math_expression
        assert normalize_math_expression("15% of 80") == "(15/100)*80"

    def test_hex_literal_is_not_multiplication(self):
        from puffpai.assistant_tools import normalize_math_expression
        assert normalize_math_expression("0x10") == "0x10"


class TestCalculateTool:
    def test_benchmark_2_2_spoken_form(self):
        tools, _ = _build_tools()
        assert tools['calculate'].invoke("What's 847 times 62?") == "52514"

    def test_symbolic_form_still_works(self):
        tools, _ = _build_tools()
        assert tools['calculate'].invoke("847 * 62") == "52514"

    def test_spaced_x_multiplies_but_hex_stays_hex(self):
        tools, _ = _build_tools()
        assert tools['calculate'].invoke("847 x 62") == "52514"
        assert tools['calculate'].invoke("0x10") == "16"

    def test_ast_safety_still_intact(self):
        tools, _ = _build_tools()
        out = tools['calculate'].invoke("__import__('os').system('echo hi')")
        assert "Calculation error" in out

    def test_power_bomb_still_rejected(self):
        tools, _ = _build_tools()
        assert "Calculation error" in tools['calculate'].invoke("10 ** 99999")


class TestIntentFilterBindsCalculator:
    """2.2 root cause: no filter branch ever kept the calculate tool."""

    def _filter(self, query):
        from puffpai.assistant_tools import AssistantTools
        names = [
            'calculate', 'search_knowledge_base', 'list_indexed_libraries',
            'search_my_documents', 'fetch_my_document', 'list_my_documents',
            'web_search', 'read_file', 'write_file',
            'prep_check_expiration', 'prep_view_inventory',
            'prep_days_remaining', 'prep_add_supply', 'prep_recommendations',
            'app_set_timer',
        ]
        stand_in = type('_Stand', (), {})()
        stand_in.tools = [types.SimpleNamespace(name=n) for n in names]
        kept = AssistantTools._filter_tools_by_intent(stand_in, query)
        return {t.name for t in kept}

    def test_times_binds_calculate(self):
        assert 'calculate' in self._filter("What's 847 times 62?")

    def test_symbolic_expression_binds_calculate(self):
        assert 'calculate' in self._filter("what is 847*62")

    def test_file_ops_bind_file_tools(self):
        kept = self._filter("read file notes.txt for me")
        assert {'read_file', 'write_file'} <= kept

    def test_benchmark_2_8_binds_prep_and_web(self):
        kept = self._filter(
            "Look up how long canned beans last, then check if any of "
            "our supplies expire this month")
        assert 'prep_check_expiration' in kept
        assert 'web_search' in kept

    def test_benchmark_2_7_binds_prep_days_remaining(self):
        # "How many days of food do we have?" bound nothing: the prep
        # branch keyed only on supplies/expire/canned wording, so the
        # model could not reach prep_days_remaining even on the agent
        # path (the router fix for the same phrasing is in router.py).
        kept = self._filter("How many days of food do we have?")
        assert 'prep_days_remaining' in kept

    def test_unrelated_chat_does_not_bind_calculate(self):
        assert 'calculate' not in self._filter("tell me about otters")


class TestAgentPromptRoutesMathToCalculate:
    def test_rule4_names_calculate(self):
        from puffpai.agent_graph import PuffPAIAgent
        stand_in = type('_S', (), {'ai_name': 'Puff', 'user_name': ''})()
        prompt = PuffPAIAgent._build_system_prompt(stand_in)
        assert 'calculate' in prompt
        assert 'NEVER use search_knowledge_base for arithmetic' in prompt


# ═══════════════════════════════════════════════════════════════════════════
#  2.8 — search_knowledge_base: RAGResponse is not a list
# ═══════════════════════════════════════════════════════════════════════════

class _FakeRAG:
    """DualZoneRAG stand-in returning a genuine RAGResponse."""

    def __init__(self, results):
        from puffpai.knowledge.rag import RAGResponse
        self._response = RAGResponse(query="q", results=results)

    def query(self, *a, **k):
        return self._response


class TestSearchKnowledgeBaseRAGResponse:
    def _invoke(self, results):
        tools, stand_in = _build_tools()
        stand_in._dual_zone_rag = _FakeRAG(results)
        with patched_modules({'puffpai.api_server': _NO_API}):
            return tools['search_knowledge_base'].invoke("canned bean shelf life")

    def test_benchmark_2_8_results_are_formatted_not_a_type_error(self):
        from puffpai.knowledge.rag import RAGResult, ResultSource
        out = self._invoke([RAGResult(
            text="Canned beans last 2-5 years past the printed date.",
            source=ResultSource.KNOWLEDGE, title="Food storage")])
        assert 'not iterable' not in out
        assert 'Error' not in out
        assert 'Canned beans last 2-5 years' in out

    def test_empty_response_says_nothing_found(self):
        # Also broken before the fix: a RAGResponse is always truthy, so the
        # "No relevant information found." arm was unreachable.
        out = self._invoke([])
        assert out == "No relevant information found."

    def test_in_server_process_uses_full_retrieval_stack(self):
        api = types.ModuleType('puffpai.api_server')
        api._get_rag_context = lambda q, n_results=12: {
            'context_text': 'FTS says: canned beans last 2-5 years.',
            'sources': [],
        }
        tools, _ = _build_tools()
        with patched_modules({'puffpai.api_server': api}):
            out = tools['search_knowledge_base'].invoke("canned beans")
        assert out == 'FTS says: canned beans last 2-5 years.'


# ═══════════════════════════════════════════════════════════════════════════
#  2.4 — list_indexed_libraries: answer from the real inventory
# ═══════════════════════════════════════════════════════════════════════════

class TestListIndexedLibraries:
    def test_in_server_process_delegates_to_shared_inventory(self):
        api = types.ModuleType('puffpai.api_server')
        api._get_indexed_libraries_direct = (
            lambda: "I have **69 indexed libraries** available offline:")
        tools, _ = _build_tools()
        with patched_modules({'puffpai.api_server': api}):
            out = tools['list_indexed_libraries'].invoke({'dummy': ''})
        assert '69 indexed libraries' in out

    def test_standalone_falls_back_to_manifest_file(self):
        tools, _ = _build_tools()
        with patched_modules({'puffpai.api_server': _NO_API}):
            out = tools['list_indexed_libraries'].invoke({'dummy': ''})
        # src/puffpai/indexed_libraries.json is gitignored, so a clean
        # checkout legitimately has no manifest — either legacy outcome
        # proves the tool took the file path, not the server delegation.
        assert ('Indexed libraries' in out
                or 'No indexed libraries found' in out)
        assert 'available offline' not in out  # the delegated format


class TestDirectLibraryRoute:
    """The 0.017s deterministic path benchmark 2.4 actually hit."""

    def _rows(self):
        return [
            {'name': 'RationalWiki', 'description': 'Critical encyclopedia',
             'is_upload': False, 'collection': 'kiwix_rationalwiki'},
            {'name': 'Wikipedia', 'description': '',
             'is_upload': False, 'collection': 'kiwix_wikipedia'},
            # Duplicate name (disambiguation only qualifies ZIM rows) and a
            # PDF ingestion — neither may inflate the library listing.
            {'name': 'Wikipedia', 'description': '',
             'is_upload': False, 'collection': 'kiwix_wikipedia_maxi'},
            {'name': 'meeting_notes.pdf', 'description': '',
             'is_upload': False, 'collection': 'up_meeting_notes'},
            {'name': 'test_grocery.heic', 'description': '',
             'is_upload': True, 'collection': 'up_test_grocery'},
        ]

    def test_lists_libraries_not_uploads(self, monkeypatch):
        api_server = pytest.importorskip('puffpai.api_server')
        monkeypatch.setattr(api_server, '_knowledge_libraries_rows', self._rows)
        out = api_server._get_indexed_libraries_direct()
        assert '2 indexed libraries' in out
        assert 'RationalWiki' in out
        assert out.count('**Wikipedia**') == 1
        assert 'meeting_notes.pdf' not in out
        # The upload is disclosed as an upload, never counted as a library.
        assert '1 uploaded document' in out
        assert 'test_grocery.heic' in out

    def test_matches_endpoint_when_empty(self, monkeypatch):
        api_server = pytest.importorskip('puffpai.api_server')
        monkeypatch.setattr(api_server, '_knowledge_libraries_rows', lambda: [])
        assert api_server._get_indexed_libraries_direct() == \
            "No libraries are currently indexed."
