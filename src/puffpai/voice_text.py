"""Turn a markdown reply into something Piper can read out loud.

Why this module exists
----------------------
Send ``POST /api/chat`` (the voice-reply endpoint) the prompt "give me a
simple pancake recipe with a table of ingredients and amounts" and the model
can answer, verbatim in ``message.content``::

    Simple Pancake Recipe. | Ingredient | Amount | |------------|--------|
    | All-purpose flour | 1 cup (120 g) | | Granulated sugar | 2 Tbsp (25 g) | ...

Neither of the other voice-side cleaners strips ``|`` or a table separator
row: ``voice/service.py::clean_for_voice`` deletes only ``* ` # _``, and
``api_server``'s ``_clean_response`` / ``clean_for_voice`` handle bold, headers,
code spans, links and bullets — but never a table. Piper reads the pipes.

What this does
--------------
:func:`speakable` is a pure, deterministic, model-free transform: markdown in,
spoken prose out. It owns the shapes the existing cleaners miss (tables, rulers,
ordered lists, fenced code, emoji, unit abbreviations) and is idempotent, so it
is safe to run ahead of the older regex passes rather than instead of them.

Discipline
----------
* Never touches digits, clock times or dates — a recipe's quantities and a
  calendar's times must survive the pass byte-for-byte.
* Never invents words. A table row becomes its own cells, in order; a dropped
  code fence says so out loud rather than silently vanishing.
* A reply with no markdown in it comes back unchanged apart from whitespace
  collapsing, which is what every caller already did.
"""

from __future__ import annotations

import re
from datetime import datetime

__all__ = ["speakable", "table_to_sentences", "voice_cosmetics"]


# ── fenced code ──────────────────────────────────────────────────────────────
# Dropped, not read. A spoken code block is noise; saying so is honest.
_FENCE_RE = re.compile(r"```[^\n]*\n?.*?(?:```|\Z)", re.DOTALL)
_CODE_NOTE = " (code omitted) "

# ── links / images ───────────────────────────────────────────────────────────
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_AUTOLINK_RE = re.compile(r"<https?://[^>\s]+>")
_BARE_URL_RE = re.compile(r"\bhttps?://\S+")

# ── table pieces ─────────────────────────────────────────────────────────────
#: A separator/ruler cell: ``---``, ``:---:``, ``===``.
_RULER_CELL_RE = re.compile(r"^:?[-=]{2,}:?$")
#: A whole line that is only a markdown ruler / thematic break.
_RULER_LINE_RE = re.compile(r"^\s*(?:\|?\s*:?[-=*_]{2,}:?\s*\|?)+\s*$")
#: A line that carries table cells. Two pipes is the floor so a lone "a | b"
#: sentence fragment is not mistaken for a table.
_TABLE_LINE_RE = re.compile(r"^\s*\|?[^|\n]*\|[^|\n]*\|")

# ── headers / emphasis / lists ───────────────────────────────────────────────
_HEADER_RE = re.compile(r"^\s*#{1,6}\s*(.+?)\s*#*\s*$", re.MULTILINE)
_BOLD_ITALIC_RE = re.compile(r"(\*{1,3}|_{1,3}|~{2})(?=\S)(.+?)(?<=\S)\1",
                             re.DOTALL)
_STRAY_MD_RE = re.compile(r"[*`#]+")
_BULLET_RE = re.compile(r"^\s*(?:[-+*•·]|•)\s+")
_ORDERED_RE = re.compile(r"^\s*(\d{1,2})[.)]\s+")
_BLOCKQUOTE_RE = re.compile(r"^\s*>+\s?")

_ORDINALS = ("First", "Second", "Third", "Fourth", "Fifth", "Sixth",
             "Seventh", "Eighth", "Ninth", "Tenth")

# ── emoji / pictographs ──────────────────────────────────────────────────────
# Narrow, explicit ranges rather than a category sweep: a household reply is
# full of accented names and ° signs that must survive.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")

# ── unit / abbreviation expansion ────────────────────────────────────────────
# Only the cheap, unambiguous ones. "g", "min" and "c" are deliberately absent:
# they collide with real words often enough to make a reply worse.
_UNIT_RULES = (
    (re.compile(r"(?<=\d)\s*°\s*F\b"), " degrees Fahrenheit"),
    (re.compile(r"(?<=\d)\s*°\s*C\b"), " degrees Celsius"),
    (re.compile(r"(?<=\d)\s*°(?![A-Za-z])"), " degrees"),
    (re.compile(r"(?<=\d)\s*%"), " percent"),
    (re.compile(r"\bTbsp\.?\b", re.IGNORECASE), "tablespoons"),
    (re.compile(r"\btablespoon\b"), "tablespoon"),
    (re.compile(r"\btsp\.?\b", re.IGNORECASE), "teaspoons"),
    (re.compile(r"(?<=\d)\s*oz\.?\b", re.IGNORECASE), " ounces"),
    (re.compile(r"(?<=\d)\s*lbs?\.?\b", re.IGNORECASE), " pounds"),
    (re.compile(r"(?<=\d)\s*ml\b", re.IGNORECASE), " milliliters"),
    (re.compile(r"(?<=\d)\s*kg\b", re.IGNORECASE), " kilograms"),
    (re.compile(r"\be\.g\.", re.IGNORECASE), "for example"),
    (re.compile(r"\bi\.e\.", re.IGNORECASE), "that is"),
    (re.compile(r"\bapprox\.", re.IGNORECASE), "approximately"),
    (re.compile(r"\s*&\s*"), " and "),
    (re.compile(r"\s*(?:->|→|=>)\s*"), " to "),
    # "1 tablespoons" is what a blanket plural produces; say it properly.
    (re.compile(r"\b1 (tablespoon|teaspoon|ounce|pound|kilogram)s\b"),
     r"1 \1"),
)


def _cells(line: str) -> "list[str]":
    """The cells of one markdown table row, outer pipes discarded."""
    raw = line.strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    return [c.strip() for c in raw.split("|")]


def _is_ruler_row(cells: "list[str]") -> bool:
    filled = [c for c in cells if c]
    return bool(filled) and all(_RULER_CELL_RE.match(c) for c in filled)


def table_to_sentences(rows: "list[list[str]]") -> str:
    """Speak a parsed markdown table: header, then one phrase per row.

    Two columns, the usual recipe shape, read as
    ``"Ingredients: salmon fillets, 4; olive oil, 2 tablespoons."`` Wider
    tables name each column so the value is not orphaned from its heading.
    """
    # Ruler cells are dropped per-cell as well as per-row: a table flattened
    # onto ONE line by an upstream newline-collapse arrives as a single row
    # carrying its own "|---|---|" separator in the middle of the data.
    rows = [[c for c in r if not _RULER_CELL_RE.match(c)] for r in rows]
    rows = [r for r in rows if any(c for c in r) and not _is_ruler_row(r)]
    if not rows:
        return ""
    header = rows[0]
    body = rows[1:]
    if not body:
        # A one-row table is a list of values, not a header.
        return ", ".join(c for c in header if c) + "."

    phrases = []
    for row in body:
        if len(header) == 2 and len(row) >= 2:
            phrases.append(", ".join(c for c in row[:2] if c))
        else:
            bits = []
            for index, value in enumerate(row):
                if not value:
                    continue
                name = header[index] if index < len(header) else ""
                bits.append(f"{name}: {value}" if name else value)
            if bits:
                phrases.append(", ".join(bits))
    if not phrases:
        return ""
    if len(header) == 2 and header[0]:
        lead = header[0]
        if not lead.lower().endswith("s"):
            lead += "s"
        return f"{lead}: " + "; ".join(phrases) + "."
    return "; ".join(phrases) + "."


def _collapse_tables(text: str) -> str:
    """Replace every markdown table block with spoken sentences."""
    lines = text.split("\n")
    out = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not _TABLE_LINE_RE.match(line):
            out.append(line)
            index += 1
            continue
        block = []
        while index < len(lines) and (_TABLE_LINE_RE.match(lines[index])
                                      or _RULER_LINE_RE.match(lines[index])):
            if lines[index].strip():
                block.append(_cells(lines[index]))
            index += 1
        spoken = table_to_sentences(block)
        if spoken:
            out.append(spoken)
    return "\n".join(out)


def _collapse_inline_pipes(text: str) -> str:
    """Last-resort pipe removal for a table already flattened onto one line.

    Upstream cleaners collapse newlines before this module sees the text on
    some paths, which turns a table into ``... | Flour | 1 cup | | Salt | ...``
    on a single line. Splitting on the pipes and dropping the ruler cells is
    the only honest thing left to do with it.
    """
    if text.count("|") < 2:
        return text.replace("|", " ")
    parts = []
    for cell in text.split("|"):
        cell = cell.strip()
        if not cell or _RULER_CELL_RE.match(cell):
            continue
        parts.append(cell)
    joined = ", ".join(parts)
    # "Recipe., Ingredient" reads badly; a cell that already ends a sentence
    # keeps its own boundary.
    joined = re.sub(r"([.!?]),\s+", r"\1 ", joined)
    return joined


def _speak_lists(text: str) -> str:
    """Bullets lose their markers; a numbered run is spoken as an ordinal run."""
    lines = text.split("\n")
    out = []
    ordered_run = 0
    for line in lines:
        stripped = _BLOCKQUOTE_RE.sub("", line)
        ordered = _ORDERED_RE.match(stripped)
        if ordered:
            body = _ORDERED_RE.sub("", stripped).strip()
            if body:
                word = (_ORDINALS[ordered_run]
                        if ordered_run < len(_ORDINALS) else None)
                if word:
                    body = f"{word}, {body[0].lower() + body[1:]}" \
                        if body[:1].isupper() and body[1:2].islower() else \
                        f"{word}, {body}"
                if not body.endswith((".", "!", "?", ":", ";")):
                    body += "."
                out.append(body)
                ordered_run += 1
            continue
        ordered_run = 0
        if _BULLET_RE.match(stripped):
            body = _BULLET_RE.sub("", stripped).strip()
            if body:
                if not body.endswith((".", "!", "?", ":", ";", ",")):
                    body += "."
                out.append(body)
            continue
        out.append(stripped)
    return "\n".join(out)


def speakable(text) -> str:
    """Markdown in, spoken prose out. Pure, deterministic, model-free.

    Idempotent: running it on its own output is a no-op, so it is safe ahead of
    the older ``_clean_response`` / ``clean_for_voice`` regex passes.
    """
    if not text:
        return "" if text is None or text == "" else str(text)
    text = str(text)
    if "```" in text:
        text = _FENCE_RE.sub(_CODE_NOTE, text)
    text = _IMAGE_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _AUTOLINK_RE.sub(" ", text)
    text = _BARE_URL_RE.sub(" ", text)
    text = _collapse_tables(text)
    # Rulers left behind by a table block, and standalone thematic breaks.
    text = "\n".join("" if _RULER_LINE_RE.match(line) else line
                     for line in text.split("\n"))
    text = _HEADER_RE.sub(lambda m: m.group(1).rstrip(".") + ".", text)
    text = _speak_lists(text)
    for _ in range(3):                      # nested **_bold italic_**
        new = _BOLD_ITALIC_RE.sub(r"\2", text)
        if new == text:
            break
        text = new
    text = _STRAY_MD_RE.sub("", text)
    if "|" in text:
        text = _collapse_inline_pipes(text)
    text = _EMOJI_RE.sub(" ", text)
    for pattern, replacement in _UNIT_RULES:
        text = pattern.sub(replacement, text)
    text = re.sub(r"\n{2,}", "\n", text)
    # A line break IS a sentence boundary once the text is flattened for TTS:
    # without this a stripped "**Instructions**" heading runs straight into the
    # first step ("Instructions First, preheat...").
    text = re.sub(r"(?<=[^\s.!?:;,\n])\n(?=\S)", ". ", text)
    text = " ".join(text.split())
    text = re.sub(r"([.!?]),\s+", r"\1 ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([.!?])\1{1,}", r"\1", text)
    return text.strip()


# ── spoken cosmetics ────────────────────────────────────────────────────────
#
# Four things the box would read out loud that nobody would say:
#
#   * "[Trivia — general knowledge (an adult general-knowledge audience)]"
#     before every quiz question;
#   * "Playing KEXP 90.3 Seattle, WA (AAC 160K) on the speakers." — the codec
#     belongs to the stream list, not to speech;
#   * every document in the list followed by "(userupload, 0 words)";
#   * "Good morning" at 2:41 PM, because the briefing text says so.
#
# All four are SPEECH shaping: the chat surface keeps the label, the codec and
# the metadata, which are useful on a screen. So they live here, on the voice
# path only, and each one removes text — none of them invents any.

#: "[Trivia — General Knowledge]" / "[Trivia - ...]" at the head of a line.
_LABEL_BRACKET_RE = re.compile(r"(?m)^[ \t]*\[[^\]\n]{0,80}\][ \t]*\n?")

#: "(AAC 160K)", "(MP3 128k)", "(AAC)", "(128K)" — a codec/bitrate aside.
_CODEC_RE = re.compile(
    r"\s*\((?:(?:aac|mp3|ogg|opus|flac|aac\+|he-aac|wma)\b[^)]{0,12}"
    r"|\d{2,4}\s*k(?:bps)?)\)", re.IGNORECASE)

#: "(userupload, 0 words)" / "(userupload)" / "(12 words)" after a filename.
_DOC_META_RE = re.compile(
    r"\s*\((?:[a-z][\w-]{0,20}\s*,\s*)?\d*\s*words?\)"
    r"|\s*\(userupload[^)]{0,24}\)", re.IGNORECASE)

#: Punctuation left stranded at the head of the reply by one of the strips
#: above — a leading ". " is the label's old full stop, not speech.
_LEADING_PUNCT_RE = re.compile(r"^[\s.,;:!?—–-]+")

#: A greeting that names a half of the day.
_DAYPART_GREETING_RE = re.compile(
    r"(?i)\bgood\s+(morning|afternoon|evening)\b")


#: The four buckets a household actually speaks in. The night bucket is the
#: one a three-way split has no word for: it would say "Good morning" at
#: 00:50, because every hour under 12 is "morning". There is no
#: good-part-of-the-day greeting for one in the morning, so the night hours
#: get a plain hello rather than an invented one.
def _daypart(hour: int) -> str:
    hour = int(hour) % 24
    if 5 <= hour <= 11:
        return "morning"
    if 12 <= hour <= 16:
        return "afternoon"
    if 17 <= hour <= 21:
        return "evening"
    return "night"


def daypart_greeting(hour: int) -> str:
    """"Good morning" / "Good afternoon" / "Good evening" / "Hello". Pure."""
    part = _daypart(hour)
    return "Hello" if part == "night" else "Good " + part


def voice_cosmetics(text, now=None) -> str:
    """Speech-only tidying. Never adds a word that was not already true.

    ``now`` is injectable so the greeting correction is testable; it defaults
    to the box's local clock, which is the thing the briefing got wrong.
    """
    out = str(text or "")
    if not out.strip():
        return out
    out = _LABEL_BRACKET_RE.sub("", out)
    out = _CODEC_RE.sub("", out)
    out = _DOC_META_RE.sub("", out)
    greeting = daypart_greeting((now or datetime.now()).hour)
    out = _DAYPART_GREETING_RE.sub(lambda m: greeting, out)
    # Removing the label leaves the punctuation that followed it, so the
    # quiz question would be spoken as ". Which planet…".
    # Nothing legitimate opens with punctuation.
    out = _LEADING_PUNCT_RE.sub("", out)
    return out.strip()

