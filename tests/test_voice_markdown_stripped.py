"""Markdown must never reach Piper.

A model asked for a recipe answers in markdown, for example:

    POST /api/chat (the voice-reply endpoint)
    prompt: "give me a simple pancake recipe with a table of ingredients and
             amounts"
    message.content: "Simple Pancake Recipe. | Ingredient | Amount |
      |------------|--------| | All-purpose flour | 1 cup (120 g) |..."

and the same shape in a voice-journal line::

    PUFF:... Garlic-Herb Crusted Salmon with Roasted Asparagus...
      | Ingredient | Amount | |------------|--------|
      | Salmon fillets (skin-on) | 4 (about 6 oz each) |...

Unfiltered, the literal pipes and the ``|------------|--------|`` separator row
are what Piper is handed. This file pins :mod:`puffpai.voice_text` and its hook in
``api_server.clean_for_voice``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.voice_text import speakable  # noqa: E402

#: A salmon recipe reply, in its markdown form.
SALMON = """## Garlic-Herb Crusted Salmon with Roasted Asparagus

| Ingredient | Amount |
|------------|--------|
| Salmon fillets (skin-on) | 4 (about 6 oz each) |
| Olive oil | 2 Tbsp |
| Garlic, minced | 3 cloves |
| Fresh dill | 1 Tbsp |
| Salt | 1/2 tsp |

**Instructions**

1. Preheat the oven to 400°F.
2. Pat the salmon dry and rub with the oil.
3. Roast for 12 minutes, until it flakes.

- Serve at 6:30 PM with the asparagus.

```python
print("not a recipe step")
```

See [the full method](https://example.com/salmon) 🐟"""

#: The pancake reply exactly as `/api/chat` returned it — already flattened
#: onto one line by the upstream newline collapse.
PANCAKE_ONE_LINE = (
    "Simple Pancake Recipe. | Ingredient | Amount | |------------|--------| "
    "| All-purpose flour | 1 cup (120 g) | | Granulated sugar | 2 Tbsp (25 g) "
    "| | Baking powder | 2 tsp (8 g) | | Salt | 1/2 tsp (3 g) | "
    "| Milk | 1 cup (240 ml) | | Egg | 1 large |")

MARKDOWN_CHARS = ("|", "*", "#", "`")


def test_the_salmon_reply_carries_no_markdown_to_piper():
    spoken = speakable(SALMON)
    for char in MARKDOWN_CHARS:
        assert char not in spoken, f"{char!r} survived into the spoken reply"
    assert "---" not in spoken


def test_the_salmon_table_is_spoken_as_a_list():
    spoken = speakable(SALMON)
    assert "Ingredients:" in spoken
    assert "Salmon fillets (skin-on), 4" in spoken
    assert "Olive oil, 2 tablespoons" in spoken


def test_the_pancake_one_liner_loses_its_pipes_and_its_ruler():
    spoken = speakable(PANCAKE_ONE_LINE)
    for char in MARKDOWN_CHARS:
        assert char not in spoken
    assert "------------" not in spoken
    assert "All-purpose flour" in spoken
    assert "1 cup (120 g)" in spoken


def test_numbers_times_and_dates_are_preserved():
    spoken = speakable(SALMON)
    for kept in ("400", "12 minutes", "6:30 PM", "1/2", "3 cloves"):
        assert kept in spoken, kept


def test_headers_become_a_sentence_and_ordered_steps_become_ordinals():
    spoken = speakable(SALMON)
    assert spoken.startswith("Garlic-Herb Crusted Salmon with Roasted "
                             "Asparagus.")
    assert "First, preheat the oven" in spoken
    assert "Second, pat the salmon dry" in spoken


def test_a_code_fence_is_dropped_but_said_to_be_dropped():
    spoken = speakable(SALMON)
    assert "(code omitted)" in spoken
    assert "print(" not in spoken


def test_a_link_keeps_its_text_and_loses_its_url():
    spoken = speakable(SALMON)
    assert "the full method" in spoken
    assert "example.com" not in spoken


def test_units_are_expanded_where_it_is_cheap():
    spoken = speakable(SALMON)
    assert "tablespoons" in spoken and "teaspoons" in spoken
    assert "degrees Fahrenheit" in spoken
    assert "Tbsp" not in spoken and "tsp" not in spoken


def test_a_short_plain_reply_is_returned_unchanged():
    for plain in ("It's 3:29 AM on Thursday, September 03.",
                  "Pantry (1 item): ground beef: 6 pounds (Protein)",
                  "3 of 12 lights are on: Kitchen Light, Porch Light.",
                  "The living room is 71.6 degrees."):
        assert speakable(plain) == plain, plain


def test_the_cleaner_is_idempotent():
    once = speakable(SALMON)
    assert speakable(once) == once


def test_empty_and_none_are_safe():
    assert speakable("") == ""
    assert speakable(None) == ""


# ── the api_server hook ──────────────────────────────────────────────────────

def test_clean_for_voice_strips_the_table_on_the_voice_path():
    from puffpai.api_server import clean_for_voice
    spoken = clean_for_voice(PANCAKE_ONE_LINE)
    for char in MARKDOWN_CHARS:
        assert char not in spoken, f"{char!r} reached the TTS text"


def test_a_truncated_voice_reply_only_promises_the_screen_when_it_is_there():
    """The default tail must stay honest; the screen wording is opt-in."""
    from puffpai import api_server
    long_reply = ("This is a complete sentence about the house. " * 40)
    default = api_server.clean_for_voice(long_reply, max_length=200)
    assert default.endswith(api_server.VOICE_TRUNCATION_TAIL.strip())
    screened = api_server.clean_for_voice(
        long_reply, max_length=200,
        tail=api_server.VOICE_TRUNCATION_TAIL_SCREEN)
    assert screened.endswith("I've put the rest on the screen.")
