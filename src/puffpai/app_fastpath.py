"""A deterministic chat path for declarative apps, driven only by the manifest.

The grammar, the bounds and the read-back rule are BuildBot's app fastpath,
unchanged; what is this tree's is WHERE it is consulted (one call inside
``api_server._try_instant_answer``, behind every hand-written household fastpath
and ahead of the registry-driven reads and the model - see
``api_server._try_app_fastpath``) and WHICH apps own their own phrasing
(:data:`HANDWRITTEN`).

Without it, a scaffolded persona is understood and then dropped. "add tomatoes
to the plantings list" reaches the registry reads, is answered with a blurb or a
recital, or falls through to the model - so on a machine with no Ollama the
answer to a sentence about the owner's own app is a 503, although the app is
installed, its store is there and the words are in the router. Nothing is
missing except the six lines that write the item down.

This module is those lines, and it is deliberately narrow:

* **Manifest-driven, never hand-coded.** The lists come from ``data.default``
  and the words from ``vocabulary``. No app id appears in a rule here, so an app
  scaffolded ten minutes ago is handled exactly as well as one shipped a year
  ago. Nothing is imported from the manifest and nothing in it is executed.
* **It never touches the hand-written household apps.** AlarmFlow, Calendar,
  FreshTable, FitFam and Trivia have their own parsers, their own ``_try_*``
  fastpaths and their own Python methods, and a second parser guessing at them is
  how "add milk" ends up in two places. So does the media hub, which is a core
  surface rather than an installable app. They are excluded by name
  (:data:`HANDWRITTEN`).
* **Evidence or silence.** Nothing is claimed until the document has been read
  back off disk and the item is in it. A write that did not land is reported as
  a write that did not land.
* **Conservative, and it asks.** One verb, one list, one item. A sentence that
  names no list when there are two is answered with the question ("Which list -
  plantings or harvest log?"), never with a guess.

The grammar, in full:

    add <item> to|on|in <list>        put <item> on <list>
    what is on <list> / show <list> / list <list> / read <list>
    remove|delete|take <item> from|off <list>
    how many ... on|in <list>

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

#: The apps that own their own phrasing in Python. This module is for the apps
#: nobody wrote code for, which is every app an owner adds.
#:
#: The five shipped household apps, by id, plus ``mediahub``: it has no manifest
#: today (it is a core surface in :class:`puffpai.app_data.AppData`, not an
#: installable app), and naming it here means that if it ever gets one, the media
#: fastpaths keep their sentences.
HANDWRITTEN = frozenset({"alarmflow", "calendar", "freshtable", "fitfam",
                         "trivia", "mediahub"})

#: How long an item may be. A list entry is a line, not a document.
MAX_ITEM_CHARS = 120

#: How many entries a reply reads back before it stops and says how many more.
MAX_READ_BACK = 20

_ARTICLES_RE = re.compile(r"^(?:a|an|the|some|my|our)\s+")
_ADD_VERBS = r"add|put|log|jot\s+down|write\s+down|note\s+down|record"
_REMOVE_VERBS = r"remove|delete|take|cross\s+off|drop|scratch"
_SHOW_VERBS = (r"what(?:'s|s|\s+is|\s+are)?|show(?:\s+me)?|list|read(?:\s+me)?"
               r"|tell\s+me|give\s+me")


def _lists_of(manifest: dict[str, Any]) -> list[str]:
    """The keys in this app's declared default shape whose value is a list."""
    block = manifest.get("data")
    default = block.get("default") if isinstance(block, dict) else None
    if not isinstance(default, dict):
        return []
    return [str(key) for key, value in default.items() if isinstance(value, list)]


def _variants(key: str) -> list[str]:
    """The spellings of one list key a person might type.

    ``harvest_log`` is said "harvest log"; a singular list is asked about in the
    plural and the other way round. A one-character equivalence, not a stemmer:
    deterministic, offline, and impossible to be surprised by.
    """
    label = str(key).replace("_", " ").strip()
    out = {label}
    if label.endswith("s"):
        out.add(label[:-1])
    else:
        out.add(label + "s")
    return sorted((v for v in out if len(v) >= 3), key=len, reverse=True)


def _candidates(manifests: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    """Every ``(manifest, list key)`` pair this module is allowed to answer for."""
    out: list[tuple[dict[str, Any], str]] = []
    for manifest in manifests:
        app_id = str(manifest.get("id") or "")
        if not app_id or app_id in HANDWRITTEN:
            continue
        for key in _lists_of(manifest):
            out.append((manifest, key))
    return out


def _mentions_app(lower: str, manifest: dict[str, Any]) -> bool:
    """Does the message use one of this app's own declared words?"""
    vocabulary = manifest.get("vocabulary")
    words = (vocabulary.get("keywords") or []) if isinstance(vocabulary, dict) else []
    for word in words:
        if isinstance(word, str) and len(word) >= 3 and word.lower() in lower:
            return True
    return False


def _handwritten_mentioned(lower: str, manifests: list[dict[str, Any]]) -> bool:
    """Does this sentence use one of the hand-written apps' own words?

    Those apps have their own parsers and their own Python methods. A message
    that names one of them is theirs even if it also looks like list-keeping, so
    this module stands down rather than answering over the top of them.
    """
    for manifest in manifests:
        if str(manifest.get("id") or "") in HANDWRITTEN and _mentions_app(lower, manifest):
            return True
    return False


def _alternation(keys: list[str]) -> str:
    spellings: list[str] = []
    for key in keys:
        spellings.extend(_variants(key))
    spellings.sort(key=len, reverse=True)
    return "|".join(re.escape(s) for s in spellings)


def _named(lower: str, pairs: list[tuple[dict[str, Any], str]]
           ) -> list[tuple[dict[str, Any], str]]:
    """The candidate lists this message names, longest spelling winning.

    A message naming "harvest log" must not also count as naming "log": the
    longer spelling is matched first and the span it used is blanked out.
    """
    haystack = f" {lower} "
    found: list[tuple[dict[str, Any], str]] = []
    ordered = sorted(
        pairs, key=lambda pair: len(max(_variants(pair[1]), key=len)), reverse=True)
    for manifest, key in ordered:
        for spelling in _variants(key):
            match = re.search(rf"\b{re.escape(spelling)}\b", haystack)
            if match:
                haystack = (haystack[:match.start()] + " " * (match.end() - match.start())
                            + haystack[match.end():])
                found.append((manifest, key))
                break
    return found


def _label(manifest: dict[str, Any], key: str) -> str:
    return str(key).replace("_", " ").strip()


def _which_question(pairs: list[tuple[dict[str, Any], str]]) -> str:
    """The ask-back for a sentence that did not say which list.

    Lists from two different apps are named with their app, because "plantings"
    twice is not a question anybody can answer.
    """
    one_app = len({str(m.get("id")) for m, _ in pairs}) == 1
    names = [
        _label(manifest, key) if one_app
        else f"{manifest.get('name') or manifest.get('id')}'s {_label(manifest, key)}"
        for manifest, key in pairs
    ]
    if len(names) == 1:
        return f"Which list? The one I keep is {names[0]}."
    return "Which list - " + ", ".join(names[:-1]) + f" or {names[-1]}?"


def _clean_item(raw: str) -> str:
    item = " ".join(str(raw or "").split())
    item = item.strip(" .!?,;:'\"")
    item = _ARTICLES_RE.sub("", item).strip()
    return item[:MAX_ITEM_CHARS].strip()


def _row_text(row: Any) -> str:
    """What one stored row says, for reading a list back in a sentence."""
    if isinstance(row, str):
        return row.strip()
    if isinstance(row, dict):
        for field in ("item", "name", "title", "text", "description", "label"):
            value = row.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _read(app_data: Any, app_id: str, key: str) -> Optional[list[Any]]:
    """The live rows of one list, or None when the document cannot be read."""
    try:
        document = app_data.get_data(app_id)
    except Exception:
        return None
    if not isinstance(document, dict):
        return None
    rows = document.get(key)
    return rows if isinstance(rows, list) else []


def _write(app_data: Any, app_id: str, key: str, rows: list[Any]) -> bool:
    try:
        document = app_data.get_data(app_id)
        if not isinstance(document, dict):
            return False
        document[key] = rows
        return bool(app_data.set_data(app_id, document))
    except Exception:
        return False


def _count_phrase(n: int, label: str) -> str:
    if n == 0:
        return f"nothing on the {label} list"
    if n == 1:
        return f"one thing on the {label} list"
    return f"{n} things on the {label} list"


def handle(message: Any, app_data: Any = None,
           manifests: Optional[list[dict[str, Any]]] = None) -> Optional[str]:
    """Answer a declarative-app sentence deterministically, or return None.

    None means "this was not one of ours" and the caller carries on to the model.
    Never raises: a broken store, an unreadable manifest or a sentence this
    grammar does not cover is a None or a plain sentence, never a traceback in
    the owner's chat.
    """
    if not isinstance(message, str) or not message.strip():
        return None
    try:
        return _handle(message, app_data, manifests)
    except Exception:  # pragma: no cover - the floor; chat must not crash
        return None


def _handle(message: str, app_data: Any,
            manifests: Optional[list[dict[str, Any]]]) -> Optional[str]:
    from puffpai import app_registry

    if manifests is None:
        manifests = app_registry.current_manifests()
    pairs = _candidates(manifests)
    if not pairs:
        return None

    lower = " ".join(message.lower().split())
    named = _named(lower, pairs)

    if named:
        # The verb is read from the sentence with the LIST NAME taken out of it.
        # A list called "harvest log" contains the word "log", which is also a
        # verb for writing something down, so "show me the harvest log" read as
        # "add" until the name stopped being part of the search.
        verb = _verb(_without_lists(lower, named))
        if verb is None:
            return None
    else:
        verb = _verb(lower)
        if verb is None:
            return None
        # No list named. One list on the whole build is unambiguous; anything
        # else gets the question, and only when the message is clearly about one
        # of these apps at all.
        if _handwritten_mentioned(lower, manifests):
            # "add milk to the shopping list" is FreshTable's sentence. This
            # module does not get to answer for an app that has its own words.
            return None
        if len(pairs) == 1:
            named = list(pairs)
        else:
            mentioned = [p for p in pairs if _mentions_app(lower, p[0])]
            if len(mentioned) == 1:
                named = mentioned
            elif mentioned:
                return _which_question(mentioned)
            elif re.search(r"\blists?\b", lower):
                # A list verb and the bare word "list". Which one is a question,
                # not a guess.
                return _which_question(pairs)
            else:
                return None

    if len(named) > 1:
        return _which_question(named)

    manifest, key = named[0]
    app_id = str(manifest["id"])
    label = _label(manifest, key)
    if app_data is None:
        from puffpai.app_data import AppData
        app_data = AppData()

    if verb == "add":
        return _do_add(lower, app_data, app_id, key, label)
    if verb == "remove":
        return _do_remove(lower, app_data, app_id, key, label)
    if verb == "count":
        rows = _read(app_data, app_id, key)
        if rows is None:
            return _unreadable(label)
        return _count_phrase(len(rows), label).capitalize() + "."
    return _do_show(app_data, app_id, key, label)


def _without_lists(lower: str, pairs: list[tuple[dict[str, Any], str]]) -> str:
    """The sentence with the named list spellings blanked out."""
    out = lower
    for _manifest, key in pairs:
        for spelling in _variants(key):
            out = re.sub(rf"\b{re.escape(spelling)}\b", " ", out)
    return " ".join(out.split())


def _verb(lower: str) -> Optional[str]:
    """Which of the four verbs this sentence uses, or None.

    Order matters. "how many" is asked first because it is also a show. A show
    verb only counts at the START of the sentence, which is where a question
    puts it; that is what keeps "log a reading" (an add whose verb is also a
    noun) from being read as a request to read something out.
    """
    if re.search(r"\bhow many\b", lower):
        return "count"
    if re.search(rf"^(?:{_SHOW_VERBS})\b", lower):
        return "show"
    if re.search(rf"\b(?:{_REMOVE_VERBS})\b", lower):
        return "remove"
    if re.search(rf"\b(?:{_ADD_VERBS})\b", lower):
        return "add"
    return None


def _unreadable(label: str) -> str:
    return (f"I could not read the {label} list off disk, so I am not going to "
            f"tell you what is on it.")


def _do_add(lower: str, app_data: Any, app_id: str, key: str,
            label: str) -> Optional[str]:
    pattern = rf"\b(?:{_ADD_VERBS})\s+(?P<item>.+?)\s+(?:to|on|onto|in|into)\s+" \
              rf"(?:the\s+|my\s+|our\s+)?(?:{_alternation([key])})\b"
    match = re.search(pattern, lower)
    if match is None:
        # "add to the plantings list" - the verb and the list, no item.
        return (f"What should I add to the {label} list? Say it as \"add "
                f"tomatoes to the {label} list\".")
    item = _clean_item(match.group("item"))
    if not item:
        return (f"What should I add to the {label} list? Say it as \"add "
                f"tomatoes to the {label} list\".")

    rows = _read(app_data, app_id, key)
    if rows is None:
        return (f"I could not read the {label} list off disk, so I did not "
                f"write to it either. Nothing was saved.")
    rows = list(rows) + [{"item": item, "added": datetime.now().isoformat()}]
    if not _write(app_data, app_id, key, rows):
        return (f"I could not save that to the {label} list, so nothing was "
                f"written down.")

    # Read-back verify. "Saved" is a claim, and a claim needs the document.
    after = _read(app_data, app_id, key)
    if after is None or not any(_row_text(row) == item for row in after):
        return (f"I tried to add {item} to the {label} list and it is not in "
                f"the saved document, so treat it as not saved.")
    return (f"Added {item} to the {label} list. That is "
            f"{_count_phrase(len(after), label)}.")


def _do_remove(lower: str, app_data: Any, app_id: str, key: str,
               label: str) -> Optional[str]:
    pattern = rf"\b(?:{_REMOVE_VERBS})\s+(?P<item>.+?)\s+(?:from|off|out of)\s+" \
              rf"(?:the\s+|my\s+|our\s+)?(?:{_alternation([key])})\b"
    match = re.search(pattern, lower)
    if match is None:
        return (f"What should I take off the {label} list? Say it as \"remove "
                f"tomatoes from the {label} list\".")
    item = _clean_item(match.group("item"))
    if not item:
        return (f"What should I take off the {label} list? Say it as \"remove "
                f"tomatoes from the {label} list\".")

    rows = _read(app_data, app_id, key)
    if rows is None:
        return (f"I could not read the {label} list off disk, so I did not "
                f"change it.")
    kept = [row for row in rows if _row_text(row).lower() != item.lower()]
    if len(kept) == len(rows):
        return (f"{item} is not on the {label} list, so nothing was removed.")
    if not _write(app_data, app_id, key, kept):
        return (f"I could not save the {label} list, so {item} is still on it.")
    after = _read(app_data, app_id, key)
    if after is None or any(_row_text(row).lower() == item.lower() for row in after):
        return (f"I tried to take {item} off the {label} list and it is still "
                f"in the saved document, so treat it as not removed.")
    return (f"Took {item} off the {label} list. That leaves "
            f"{_count_phrase(len(after), label)}.")


def _do_show(app_data: Any, app_id: str, key: str, label: str) -> str:
    rows = _read(app_data, app_id, key)
    if rows is None:
        return _unreadable(label)
    texts = [_row_text(row) for row in rows]
    texts = [t for t in texts if t]
    if not texts:
        if rows:
            return (f"The {label} list has {len(rows)} entries, but none of "
                    f"them say what they are in words I can read out. The page "
                    f"at the app shows them as they are stored.")
        return f"The {label} list is empty."
    shown = texts[:MAX_READ_BACK]
    more = len(texts) - len(shown)
    sentence = f"On the {label} list: " + ", ".join(shown) + "."
    if more:
        sentence += f" And {more} more."
    return sentence


__all__ = ["HANDWRITTEN", "handle"]
