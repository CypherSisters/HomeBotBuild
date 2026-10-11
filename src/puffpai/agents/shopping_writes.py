"""The shopping list has ONE store, and a write to it is confirmed by a read.

Without it, "add oat milk to the shopping list, then tell me how many items
are on the list" can be answered "Added oat milk to the shopping list. There
are five items listed." with neither half true: FreshTable's
``shoppingList`` never sees the item, and "five" is invented. Two separate
causes, one class of bug: prose that describes an action instead of
reporting one:

* a write to Home Assistant's ``shopping_list`` integration lands where
  NOTHING in this tree reads, while the app, the dashboard tile,
  ``app_add_shopping`` and the document ingester all use FreshTable; and
* a confirmation from a template keyed on the INTENT reads the same whether
  the handler wrote anything or not.

This module owns both halves for every surface: the canonical write plus its
read-back (:func:`add_verified`), and the sentence composed from that evidence
(:func:`reply_for`). The Steward, the Concierge's renderer and the chat
fastpath all come through here, so the three cannot drift into three different
accounts of the same list.
"""

from __future__ import annotations

import re

from puffpai.logging_config import get_logger

logger = get_logger(__name__)

#: Longest utterance the fastpath will claim. A paragraph that mentions the
#: shopping list is a conversation, not a one-item add.
MAX_LEN = 160


def _store(app=None):
    if app is not None:
        return app
    from puffpai.app_data import AppData
    return AppData()


def count_items(app=None) -> "int | None":
    """How many items the canonical list holds, or None if it can't be read."""
    try:
        return len(_store(app).fresh_shopping_items() or [])
    except Exception as exc:
        logger.debug("shopping count failed: %s", exc)
        return None


def add_verified(item: str, app=None, ha=None) -> dict:
    """Write *item* to FreshTable's shopping list and READ IT BACK.

    Returns evidence, never a claim::

        {ok, list, store, item, written, verified, count, mirrored, error}

    ``written`` says the write call returned; ``verified`` says the item is in
    the store on a fresh read; ``count`` is the store's own count after the
    write. Only ``verified`` may be spoken as "Added".

    *ha* is an optional Home Assistant bridge. When the canonical write is
    verified, the item is mirrored to HA's ``shopping_list`` so it also shows
    in the HA companion app for households that use it. The mirror is
    best-effort by design: it is not the list this appliance displays, so its
    failure must not turn a successful add into an apology.
    """
    written = verified = mirrored = False
    count, error = None, None
    store = None
    item = str(item or "").strip()
    if not item:
        return {"ok": False, "list": "shopping", "store": "freshtable",
                "item": item, "written": False, "verified": False,
                "count": None, "mirrored": False,
                "error": "no item name"}

    try:
        store = _store(app)
        store.fresh_add_shopping(item, 1, 0)
        written = True
    except Exception as exc:
        error = str(exc) or exc.__class__.__name__
        logger.warning("shopping write failed for %r: %s", item, exc)

    if written and store is not None:
        try:
            rows = store.fresh_shopping_items() or []
            count = len(rows)
            wanted = item.lower()
            verified = any(
                str(r.get("name") or "").strip().lower() == wanted
                for r in rows)
            if not verified:
                error = error or "item absent from the list on read-back"
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            logger.warning("shopping read-back failed for %r: %s", item, exc)
    elif written:
        error = error or "no store to read back from"

    if verified and ha is not None:
        try:
            ha.call_service("shopping_list", "add_item", name=item)
            mirrored = True
        except Exception as exc:
            logger.debug("HA shopping-list mirror failed for %r: %s",
                         item, exc)

    return {"ok": True, "list": "shopping", "store": "freshtable",
            "service": "freshtable.fresh_add_shopping", "item": item,
            "written": written, "verified": verified, "count": count,
            "mirrored": mirrored, "error": error}


def reply_for(result: dict) -> str:
    """The sentence the evidence in *result* supports. One owner, three callers."""
    item = (result or {}).get("item") or "that"
    if not (result or {}).get("verified"):
        return (f"I couldn't add {item} to the shopping list — "
                "the list is unchanged. Please try again.")
    count = result.get("count")
    if isinstance(count, int):
        unit = "item" if count == 1 else "items"
        return (f"Added {item} to the shopping list. "
                f"That's {count} {unit} on it now.")
    return f"Added {item} to the shopping list."


#: "add oat milk to the shopping list" and the ways a household says it. The
#: list noun is REQUIRED — a bare "add oat milk" belongs to whatever context
#: the conversation was already in, not to this fastpath.
_ADD_RE = re.compile(
    r"\b(?:add|put|stick|throw|toss)\b\s+(?P<item>.+?)\s+"
    r"(?:on|to|onto|in|into)\s+(?:the\s+|my\s+|our\s+)?"
    r"(?:shopping|grocery|groceries)\s*(?:list)?\s*[.!?]*\s*$"
    r"|\b(?:add|put)\b\s+(?P<item2>.+?)\s+"
    r"(?:to|on)\s+(?:the\s+|my\s+|our\s+)?list\s+"
    r"(?:at|from|for)\s+the\s+(?:store|market)\s*[.!?]*\s*$",
    re.IGNORECASE)

#: Words that make the same sentence something other than a one-item add.
_ADD_EXCLUDE_RE = re.compile(
    r"(?i)\bremove\b|\bdelete\b|\btake\b\s+\w+\s+\boff\b|\bcheck\s+off\b"
    r"|\bcross\s+off\b|\bclear\b|\bempty\b|\bhow\s+many\b|\bwhat'?s\b"
    r"|\border\b|\bevery\b|\bremind\s+me\b")


def match(user_message: str) -> "str | None":
    """The item this utterance adds to the shopping list, or None. Pure."""
    text = (user_message or "").strip()
    if not text or len(text) > MAX_LEN:
        return None
    if _ADD_EXCLUDE_RE.search(text):
        return None
    m = _ADD_RE.search(text)
    if not m:
        return None
    item = (m.group("item") or m.group("item2") or "").strip(" .,!?\"'")
    # "some milk", "a dozen eggs" keep their words; only the empty match and
    # the bare determiner are rejected.
    if len(item) < 2 or item.lower() in ("it", "that", "this", "them",
                                         "those", "these", "something"):
        return None
    return item


# ── removing ────────────────────────────────────────────────────────────
#
# The inverse of the add needs a deterministic owner too: without it "remove
# oat milk from the shopping list" and "cross oat milk off the list" fall
# through to a READ and the appliance recites the whole list back. Same shape
# as the add: one store, one read-back, and a sentence built only from what
# the list holds afterwards.

_REMOVE_RE = re.compile(
    r"\b(?:remove|delete|drop)\s+(?P<item>.+?)\s+(?:from|off\s+of|off)\s+"
    r"(?:the\s+|my\s+|our\s+)?(?:shopping|grocery|groceries)?\s*list\b"
    r"|\b(?:take|cross|check|scratch|strike)\s+(?P<item2>.+?)\s+"
    r"(?:off|off\s+of)\s+(?:the\s+|my\s+|our\s+)?"
    r"(?:shopping|grocery|groceries)?\s*list\b"
    r"|\b(?:remove|delete|take\s+off|cross\s+off)\s+(?P<item3>.+?)\s+"
    r"from\s+(?:the\s+|my\s+|our\s+)?(?:shopping|grocery|groceries)\b"
    # "cross nothing off" names no list; if the shape matched nothing, the
    # turn would fall to RAG and the library would answer with an invented
    # article about people named Cross. These four verbs are
    # shopping-list idiom on their own, so the list noun is optional after
    # them. Last in the alternation, so a phrasing that DOES name the list
    # still matches through the branches above it.
    r"|\b(?:cross|scratch|strike|check)\s+(?P<item4>.+?)\s+off\b",
    re.IGNORECASE)


#: The object is a pronoun — the shape is a remove, but nothing was named.
_VAGUE_OBJECTS = frozenset({"it", "that", "this", "them", "those", "these",
                            "something", "anything", "one", "the item"})

#: "take everything off the list" is a different, much larger action.
_BULK_OBJECTS = frozenset({"everything", "all", "it all", "them all",
                           "the whole list", "all of it", "the list"})


def remove_shape(user_message: str) -> "str | None":
    """The raw object of a remove phrasing, however useless. Pure.

    Separate from :func:`match_remove` so the fastpath can CLAIM a turn whose
    shape is unmistakable but whose object it cannot use — the alternative is
    the library answering "cross X off" with an encyclopaedia article.
    """
    text = (user_message or "").strip()
    if not text or len(text) > MAX_LEN:
        return None
    m = _REMOVE_RE.search(text)
    if not m:
        return None
    return (m.group("item") or m.group("item2") or m.group("item3")
            or m.group("item4") or "").strip(" .,!?\"'")


def match_remove(user_message: str) -> "str | None":
    """The item this utterance takes OFF the shopping list, or None. Pure."""
    text = (user_message or "").strip()
    if not text or len(text) > MAX_LEN:
        return None
    item = remove_shape(text)
    if item is None:
        return None
    low = item.lower()
    if len(item) < 2 or low in _VAGUE_OBJECTS or low in _BULK_OBJECTS:
        return None
    return item


def remove_verified(item: str, app=None) -> dict:
    """Take *item* off FreshTable's shopping list and READ THE LIST BACK.

    Returns evidence, never a claim::

        {ok, list, item, removed, ambiguous, count, error}

    ``removed`` is the names the store actually dropped; ``count`` is what the
    list holds afterwards. An item that was never there is reported as absent,
    not apologised for, and an ambiguous name removes NOTHING.
    """
    item = str(item or "").strip()
    result = {"ok": False, "list": "shopping", "store": "freshtable",
              "item": item, "removed": [], "ambiguous": [], "count": None,
              "error": None}
    if not item:
        result["error"] = "no item name"
        return result
    try:
        store = _store(app)
        removed, ambiguous = store.fresh_remove_shopping(item)
        result["removed"] = list(removed or [])
        result["ambiguous"] = list(ambiguous or [])
        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc) or exc.__class__.__name__
        logger.warning("shopping remove failed for %r: %s", item, exc)
        return result
    result["count"] = count_items(app)
    return result


def remove_reply_for(result: dict) -> str:
    """The sentence the evidence in *result* supports."""
    item = (result or {}).get("item") or "that"
    if not (result or {}).get("ok"):
        return (f"I couldn't take {item} off the shopping list — "
                "the list is unchanged.")
    ambiguous = result.get("ambiguous") or []
    if ambiguous:
        names = ", ".join(ambiguous[:5])
        return (f"There's more than one match for {item} on the list: {names}. "
                "Which one should I take off?")
    removed = result.get("removed") or []
    if not removed:
        return f"{str(item)[:1].upper()}{str(item)[1:]} wasn't on the list."
    name = removed[0] if len(removed) == 1 else ", ".join(removed)
    count = result.get("count")
    sentence = f"Took {name} off the shopping list."
    if isinstance(count, int):
        unit = "item" if count == 1 else "items"
        sentence += f" That's {count} {unit} now."
    return sentence


def handle_remove(user_message: str, app=None) -> "str | None":
    """Fastpath: remove the named item, verified, or None to fall through."""
    item = match_remove(user_message)
    if item is None:
        # The shape IS a remove and only the object is unusable. Answer
        # it — a question costs one turn, and the alternative is the library
        # inventing an answer about a name that was never on the list.
        raw = remove_shape(user_message)
        if raw is None:
            return None
        if raw.lower() in _BULK_OBJECTS:
            return ("I'd rather not clear the whole shopping list in one go — "
                    "tell me which items to take off and I'll do those.")
        return "Which item should I take off the shopping list?"
    return remove_reply_for(remove_verified(item, app=app))


def handle(user_message: str, app=None, ha=None) -> "str | None":
    """Fastpath: add the named item, verified, or None to fall through."""
    item = match(user_message)
    if item is None:
        return None
    result = add_verified(item, app=app, ha=ha)
    if not result.get("ok"):
        return None
    return reply_for(result)
