"""
Recipe file ingest — turn a user-loaded file into recipe candidates.

A photo of a recipe card, a PDF cookbook page, a screenshot pasted into a doc:
one LLM pass over the file's text pulls out the recipe(s) as strict JSON.

Nothing here WRITES to FreshTable. Every function returns candidate recipe
dicts shaped for AppData.fresh_create_recipe:
    {name, ingredients (comma-joined string), instructions, yield, prep_time}
The dashboard shows candidates to the user, who confirms which to save — an
LLM's reading of a photo never lands in the recipe box unreviewed. Same trust
model as calendar_ingest.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

import json
import re

# Hard cap per file: a scanned cookbook chapter shouldn't flood the UI.
MAX_RECIPES = 5

# Field length caps — a garbled OCR pass can produce runaway strings, and a
# recipe suggestion re-serves these candidates on every /pending poll.
_NAME_CAP = 200
_INGREDIENTS_CAP = 4000
_INSTRUCTIONS_CAP = 8000
_SHORT_CAP = 120  # yield / prep_time


def _coerce_ingredients(value):
    """Recipe ingredients as a comma-joined string.

    The model is asked for a comma-separated string, but often returns a list
    anyway; accept either.
    """
    if isinstance(value, list):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return ", ".join(parts)
    return str(value or "").strip()


def _short_or_none(value):
    s = str(value).strip()[:_SHORT_CAP] if value else ""
    return s or None


def _clean_recipe(raw):
    """Validate/coerce one candidate; None if unusable.

    A recipe needs at minimum a name and something to cook with — a candidate
    missing either is dropped rather than saved as a blank card.
    """
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or raw.get("title") or "").strip()
    ingredients = _coerce_ingredients(raw.get("ingredients"))
    if not name or not ingredients:
        return None
    instructions = str(raw.get("instructions") or raw.get("steps") or "").strip()
    return {
        "name": name[:_NAME_CAP],
        "ingredients": ingredients[:_INGREDIENTS_CAP],
        "instructions": instructions[:_INSTRUCTIONS_CAP],
        "yield": _short_or_none(raw.get("yield") or raw.get("servings")),
        "prep_time": _short_or_none(raw.get("prep_time") or raw.get("time")),
    }


# ── LLM extraction ───────────────────────────────────────────────────────────

_LLM_PROMPT = """Extract every recipe from the document below.

Reply with ONLY a JSON array, no prose. Each element:
  {{"name": str, "ingredients": str (a single COMMA-SEPARATED string, e.g.
    "2 cups flour, 1 tsp salt, 3 eggs"), "instructions": str (the steps,
    newline-separated), "yield": str|null (servings, e.g. "4 servings"),
    "prep_time": str|null (e.g. "30 minutes")}}
Keep ingredients as one comma-separated string, never a list of objects.
If there are no recipes, reply [].

DOCUMENT:
{document}
"""


def llm_extract(text, llm_chat):
    """Ask the LLM for recipes. `llm_chat(messages) -> str`. Returns
    (recipes, notes)."""
    doc = text[:12000]
    prompt = _LLM_PROMPT.format(document=doc)
    # A raising llm_chat propagates to ingest_text, which degrades to
    # method "none" — a recipe read that failed is not "the AI found nothing".
    reply = llm_chat([{"role": "user", "content": prompt}]) or ""
    m = re.search(r"\[.*\]", reply, re.DOTALL)
    if not m:
        return [], ["The AI found no recipe in this file."]
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        return [], ["The AI's answer wasn't valid JSON — try a cleaner file."]
    if not isinstance(parsed, list):
        return [], ["The AI found no recipe in this file."]
    recipes, dropped = [], 0
    for item in parsed[:MAX_RECIPES]:
        cleaned = _clean_recipe(item)
        if cleaned:
            recipes.append(cleaned)
        else:
            dropped += 1
    notes = ["These were read by the AI — check them before saving."]
    if dropped:
        notes.append(f"{dropped} suggestion(s) dropped (missing name/ingredients).")
    if len(text) > 12000:
        notes.append("Long document — only the first part was read.")
    return recipes, notes


# ── Dispatch ─────────────────────────────────────────────────────────────────

def ingest_text(filename, text, llm_chat=None):
    """Parse file content into recipe candidates. Returns
    {method, recipes, notes}. Never raises."""
    if llm_chat is None:
        return {"method": "none", "recipes": [],
                "notes": ["Reading recipes needs AI extraction, "
                          "which is unavailable right now."]}
    try:
        recipes, notes = llm_extract(text or "", llm_chat)
    except Exception as e:
        return {"method": "none", "recipes": [],
                "notes": [f"Recipe extraction failed: {e}"]}
    return {"method": "llm", "recipes": recipes, "notes": notes}
