"""Tests for puffpai.recipe_ingest — LLM extraction of recipe candidates.

No test here talks to a real LLM: llm_chat is always a stub. A recipe
suggestion re-serves these candidates on every /pending poll, so field caps and
never-raise behaviour matter as much as the happy path.
"""

import json
import os
import sys

_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from puffpai import recipe_ingest as ri


def _llm(reply):
    """Stub llm_chat returning a canned string."""
    return lambda messages: reply


_TWO_RECIPES = json.dumps([
    {"name": "Pancakes", "ingredients": "2 cups flour, 2 eggs, 1 cup milk",
     "instructions": "Mix. Cook on a griddle.", "yield": "4 servings",
     "prep_time": "15 minutes"},
    {"name": "Omelette", "ingredients": "3 eggs, salt, butter",
     "instructions": "Whisk and fry."},
])


class TestIngestText:
    def test_valid_multi_recipe(self):
        out = ri.ingest_text("brunch.pdf", "text", llm_chat=_llm(_TWO_RECIPES))
        assert out["method"] == "llm"
        assert len(out["recipes"]) == 2
        first = out["recipes"][0]
        assert first["name"] == "Pancakes"
        assert first["ingredients"] == "2 cups flour, 2 eggs, 1 cup milk"
        assert first["yield"] == "4 servings"
        assert first["prep_time"] == "15 minutes"
        # Optional fields default to None when absent.
        assert out["recipes"][1]["yield"] is None
        assert out["recipes"][1]["prep_time"] is None

    def test_ingredients_as_list_is_coerced_to_comma_string(self):
        reply = json.dumps([{"name": "Salad",
                             "ingredients": ["lettuce", "tomato", "olive oil"],
                             "instructions": "Toss."}])
        out = ri.ingest_text("salad.jpg", "x", llm_chat=_llm(reply))
        assert out["recipes"][0]["ingredients"] == "lettuce, tomato, olive oil"

    def test_missing_name_is_rejected(self):
        reply = json.dumps([
            {"ingredients": "flour, water", "instructions": "Mix."},
            {"name": "Bread", "ingredients": "flour, water, yeast"},
        ])
        out = ri.ingest_text("f.pdf", "x", llm_chat=_llm(reply))
        assert [r["name"] for r in out["recipes"]] == ["Bread"]

    def test_missing_ingredients_is_rejected(self):
        reply = json.dumps([{"name": "Mystery", "instructions": "Somehow."}])
        out = ri.ingest_text("f.pdf", "x", llm_chat=_llm(reply))
        assert out["recipes"] == []

    def test_caps_at_max_recipes(self):
        many = json.dumps([
            {"name": f"Dish {i}", "ingredients": "a, b"} for i in range(20)
        ])
        out = ri.ingest_text("book.pdf", "x", llm_chat=_llm(many))
        assert len(out["recipes"]) == ri.MAX_RECIPES

    def test_field_lengths_are_capped(self):
        reply = json.dumps([{
            "name": "N" * 5000,
            "ingredients": "I" * 20000,
            "instructions": "S" * 40000,
            "yield": "Y" * 5000,
        }])
        r = ri.ingest_text("f.pdf", "x", llm_chat=_llm(reply))["recipes"][0]
        assert len(r["name"]) <= 200
        assert len(r["ingredients"]) <= 4000
        assert len(r["instructions"]) <= 8000
        assert len(r["yield"]) <= 120

    def test_malformed_json_returns_method_llm_with_no_recipes(self):
        out = ri.ingest_text("f.pdf", "x", llm_chat=_llm("{not json,,,"))
        assert out["method"] == "llm"
        assert out["recipes"] == []
        assert out["notes"]

    def test_non_array_reply_yields_no_recipes(self):
        out = ri.ingest_text("f.pdf", "x", llm_chat=_llm('{"name": "X"}'))
        assert out["recipes"] == []

    def test_no_llm_available_is_method_none(self):
        out = ri.ingest_text("f.pdf", "x", llm_chat=None)
        assert out["method"] == "none"
        assert out["recipes"] == []
        assert out["notes"]

    def test_llm_exception_does_not_raise(self):
        def boom(messages):
            raise RuntimeError("ollama timed out")
        out = ri.ingest_text("f.pdf", "x", llm_chat=boom)
        assert out["method"] == "none"
        assert out["recipes"] == []

    def test_empty_text_never_raises(self):
        out = ri.ingest_text("f.pdf", "", llm_chat=_llm("[]"))
        assert out["recipes"] == []
