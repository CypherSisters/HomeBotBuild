"""The librarian's voice: spoken phrasings → a real catalogue action → a
grounded sentence.

All the deterministic wording lives here so the api_server fastpath
(``_try_books``) and the ``books_*`` tools in assistant_tools stay three lines
each — the same division the media library uses (``media_library`` answers,
``_try_media_library_query`` only routes).

TWO RULES, both from a real failure (an invented Libby/Gutenberg answer
over a real 60k-row catalogue):

* **Only real rows.** Every title, author, shelf and count below came out of
  :mod:`puffpai.books.library`. A miss is said out loud, with up to three
  closest titles we ACTUALLY have.
* **Say exactly what was done.** A single-book shelf change happens
  immediately and the reply names the book and the shelf. A bulk change (more
  than :data:`BULK_LIMIT` books in one request) is NOT performed: it asks first
  (via :mod:`puffpai.pending_clarifier`, so "yes" on the next turn finishes it)
  and says nothing has been moved yet.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import logging
import re

from puffpai.books import library, show

logger = logging.getLogger(__name__)

#: How many titles one spoken answer reads out, and the truncation said aloud
#: (same discipline as ``media_library.PLAYLIST_PREVIEW``).
PREVIEW = 8

#: More books than this in ONE request is a bulk change: it is asked about,
#: never performed on the spot.
BULK_LIMIT = 5

CATALOGUE_UNAVAILABLE = (
    "I can't reach your book catalogue right now, so I can't look that up.")
SHELVES_UNAVAILABLE = (
    "I can't reach your Calibre-web shelves right now, so I can't change them.")

#: Shelf names the household already speaks without the word "shelf".
_KNOWN_SHELF_WORDS = {"toread", "readinglist", "wanttoread", "favorites",
                      "favourites", "read", "finished", "currentlyreading"}
#: ...and the lists that are emphatically NOT shelves.
_NOT_A_SHELF_RE = re.compile(
    r"\b(shopping|grocery|groceries|pantry|chore|todo|to-do|task|packing|"
    r"guest|calendar|playlist)\b", re.IGNORECASE)

_ANAPHOR_RE = re.compile(
    r"^(?:it|that|this|them|those|that one|the (?:first|second|third|fourth|"
    r"fifth|last) one|the (?:first|second|third|fourth|fifth|last))$",
    re.IGNORECASE)
_ORDINALS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4,
             "last": -1}
_BULK_RE = re.compile(r"^(?:all|every|both|any)\b", re.IGNORECASE)
#: "all books by Stephen King" — the name after "by" is an AUTHOR. Read as a
#: title it matched "…Chronicle of the Kings of England" and shelved two books
#: nobody asked for.
_BULK_BY_RE = re.compile(
    r"^(?:all|every|both|any)\b.*?\bby\s+(?P<a>.+?)\s*$", re.IGNORECASE)


# ── tool argument parsing (one string in, like every other tool here) ───────

_ARG_RE = re.compile(r"\b(author|category|subject|series|like|shelf|comics|"
                     r"mine)\s*=\s*([^|]+)", re.IGNORECASE)


def parse_args(text) -> tuple:
    """``'category=sci fi|comics=yes'`` → ``({"category": "sci fi", …}, "")``.

    Everything that is not a ``key=value`` pair comes back as the plain query,
    so a model that just passes the words still works.
    """
    raw = str(text or "").strip().strip("'\"")
    kwargs: dict = {}
    plain_parts: list = []
    for chunk in raw.split("|"):
        m = _ARG_RE.search(chunk)
        if m:
            kwargs[m.group(1).lower()] = m.group(2).strip().strip("'\"")
        elif chunk.strip():
            plain_parts.append(chunk.strip())
    return kwargs, " ".join(plain_parts).strip()


def parse_flag(value) -> bool:
    return str(value or "").strip().lower() in ("1", "y", "yes", "true", "on",
                                                "comics", "only")


def parse_pair(text) -> tuple:
    """``'Dracula|to read'`` → ``("Dracula", "to read")``; also accepts
    ``'Dracula on my to-read shelf'``."""
    raw = str(text or "").strip().strip("'\"")
    if "|" in raw:
        parts = [p.strip() for p in raw.split("|")]
        return parts[0], (parts[1] if len(parts) > 1 else "")
    m = re.search(r"^(?P<what>.+?)\s+(?:on|to|off|from|in)\s+(?:my|our|the)?\s*"
                  r"(?P<shelf>.+?)(?:\s+shelf)?\s*$", raw, re.IGNORECASE)
    if m:
        return m.group("what").strip(), m.group("shelf").strip()
    return raw, ""


# ── sentences ───────────────────────────────────────────────────────────────

def _join(names: list) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def list_reply(head: str, books: list) -> str:
    """A grounded list, with any truncation stated out loud."""
    shown = books[:PREVIEW]
    body = "; ".join(b.label for b in shown)
    hidden = len(books) - len(shown)
    tail = f" …and {hidden} more." if hidden > 0 else "."
    return f"{head}: {body}{tail}"


def miss_reply(what: str, near: list) -> str:
    """The honest miss — never a summary of a book we do not have."""
    msg = f"I don't have {what} in your book catalogue."
    if near:
        names = [b.label for b in near[:3]]
        msg += (f" The closest I have is {names[0]}." if len(names) == 1
                else f" The closest I have are {_join(names)}.")
    return msg


# ── resolving what the person pointed at ────────────────────────────────────

def _from_memory(text: str, client_id) -> list:
    remembered = library.last(client_id)
    if not remembered:
        return []
    m = re.search(r"\b(first|second|third|fourth|fifth|last)\b", text or "",
                  re.IGNORECASE)
    if m:
        idx = _ORDINALS[m.group(1).lower()]
        try:
            return [remembered[idx]]
        except IndexError:
            return []
    return [remembered[0]]


def resolve_reference(text: str, client_id=None) -> list:
    """The book(s) a phrase names: "it"/"the second one" from memory, a title
    from the catalogue, or a whole set for an explicit "all …" ask."""
    phrase = " ".join(str(text or "").split()).strip(" .,?!\"'")
    if not phrase:
        return []
    if _ANAPHOR_RE.match(phrase):
        return _from_memory(phrase, client_id)
    if _BULK_RE.match(phrase):
        by = _BULK_BY_RE.match(phrase)
        if by:
            author = " ".join(by.group("a").split()).strip(" .,?!\"'")
            return library.find(author=author, limit=200) if author else []
        body = re.sub(r"^(?:all|every|both|any)\s+(?:my|our|the)?\s*", "",
                      phrase, flags=re.IGNORECASE)
        # Only a TRAILING noun is furniture: "all my Comics books" is the
        # Comics umbrella, so the word "comics" itself has to survive.
        body = re.sub(r"\s*\b(books?|novels?|titles?)\b\s*$", "", body,
                      flags=re.IGNORECASE)
        body = " ".join(body.split())
        for umbrella in library.UMBRELLA_TAGS:
            if library._norm(umbrella) == library._norm(body):
                return library.find(tag=umbrella, limit=50)
        cat = library.resolve_category(body)
        if cat:
            return library.find(category=cat, limit=50)
        if body:
            by_author = library.find(author=body, limit=50)
            if by_author:
                return by_author
            return library.find(body, limit=50)
        return []
    title = show._clean_title(phrase) or phrase
    book = library.best_match(title)
    if book is not None:
        return [book]
    hits = library.find(title, limit=1)
    return hits[:1]


def _shelfish(name: str, said_shelf: bool) -> bool:
    """Is this target really a SHELF? Said out loud ("… shelf"), a name the
    household already uses, or a shelf Calibre-web actually has. Anything else
    — the shopping list, the calendar — is not ours to touch."""
    clean = library._shelf_key(name)
    if not clean or _NOT_A_SHELF_RE.search(name or ""):
        return False
    if said_shelf or clean in _KNOWN_SHELF_WORDS:
        return True
    return library.shelf_name(name) is not None


# ── the actions (shared by the fastpath and the tools) ──────────────────────

def do_find(query: str = "", *, category: str = "", author: str = "",
            subject: str = "", series: str = "", comics_only: bool = False,
            mine_only: bool = False, client_id=None, limit: int = 10) -> str:
    """Search the catalogue and say what is really there."""
    if not library.catalogue_available():
        return CATALOGUE_UNAVAILABLE
    cat = library.resolve_category(category) if category else ""
    if category and not cat:
        return (f"I don't have a \"{category}\" section. My categories are "
                f"things like Science Fiction, Mystery & Detective and "
                f"Survival & Preparedness.")
    total = 0
    if not query and not subject:
        # A browse: the best-known titles first, and the REAL count of what is
        # there — "502 in Survival & Preparedness", not "10".
        books, total = library.browse(category=cat, author=author,
                                      series=series, comics_only=comics_only,
                                      mine_only=mine_only, limit=limit)
    else:
        books = library.find(query, author=author, category=cat,
                             subject=subject, series=series,
                             comics_only=comics_only, mine_only=mine_only,
                             limit=limit)
    if not books and query and not (cat or author or subject or series):
        # A topic, not a title: the Library of Congress subjects, when curation
        # has created that column.
        books = library.find(subject=query, limit=limit)
    if not books:
        what = (f"anything in {cat}" if cat else
                f"anything by {author}" if author else
                f"\"{query}\"" if query else "anything like that")
        return miss_reply(what, library.nearest_titles(query) if query else [])
    library.remember_last(client_id, books)
    count = total or len(books)
    where = (cat if cat else f"{series}" if series else
             f"your catalogue by {author}" if author else
             "comics on the shelf" if comics_only else "your catalogue")
    head = f"I found {count} in {where}" if where != "comics on the shelf" \
        else f"I found {count} comics on the shelf"
    if total > len(books):
        head += "; the best known are"
    return list_reply(head, books)


def do_recommend(*, category: str = "", author: str = "", like: str = "",
                 shelf: str = "", client_id=None, limit: int = 3) -> str:
    """Suggest something to read — out of the catalogue, never invented."""
    if not library.catalogue_available():
        return CATALOGUE_UNAVAILABLE
    cat = library.resolve_category(category) if category else ""
    if category and not cat:
        return (f"I don't have a \"{category}\" section, so I can't pick from "
                f"it.")
    like_id = None
    like_book = None
    if like:
        like_book = library.best_match(like)
        if like_book is None:
            return miss_reply(f"\"{like}\"", library.nearest_titles(like))
        like_id = like_book.id
    books = library.recommend(category=cat, author=author, like_book_id=like_id,
                              shelf=shelf, limit=limit)
    if not books:
        where = (f" in {cat}" if cat else f" on {shelf}" if shelf else "")
        return f"I couldn't find anything{where} to recommend from your shelves."
    library.remember_last(client_id, books)
    if like_book is not None:
        head = f"Like {like_book.title}, I'd try"
    elif cat:
        head = f"From {cat}, I'd try"
    elif shelf:
        head = f"From your {shelf} shelf, I'd try"
    else:
        head = "I'd try"
    return list_reply(head, books)


def do_shelves(name: str = "", client_id=None) -> str:
    """List the shelves, or what is on one of them."""
    if not library.shelves_available():
        return SHELVES_UNAVAILABLE
    if not name:
        rows = library.shelves()
        if not rows:
            return ("You don't have any shelves yet. Say \"make a shelf called "
                    "to-read\" and I'll start one.")
        parts = [f"{n} ({c} book{'s' if c != 1 else ''})" for n, c in rows]
        return f"You have {len(rows)} shelves: {_join(parts)}."
    real = library.shelf_name(name)
    if real is None:
        return f"You don't have a shelf called {name}."
    books = library.shelf_books(real)
    if not books:
        return f"Your {real} shelf is empty."
    library.remember_last(client_id, books)
    return list_reply(f"{len(books)} on your {real} shelf", books)


def _bulk_apply(books: list, apply) -> str:
    """Perform a small multi-book change and say exactly what happened."""
    results = [apply(book) for book in books]
    ok = [r for r in results if r["ok"]]
    if not ok:
        return "I couldn't change those shelves."
    names = [r["book"] for r in ok]
    return f"Done — {_join(names)} are on your {ok[0]['shelf']} shelf."


def _one_change(result: dict, verb: str) -> str:
    """Say exactly what was done to one book."""
    action, book, shelf = result["action"], result["book"], result["shelf"]
    if not result["ok"]:
        return f"I couldn't {verb} that — {result['detail']}."
    if action == "already_on_shelf":
        return f"{book} is already on your {shelf} shelf."
    if action in ("not_on_shelf", "no_such_shelf"):
        return f"{book} wasn't on {shelf or 'that shelf'}, so nothing changed."
    if action == "add":
        return f"Done — {book} is on your {shelf} shelf."
    if action == "remove":
        return f"Done — I took {book} off your {shelf} shelf."
    if action == "move":
        tail = f" ({result['detail']})" if result["detail"] else ""
        return f"Done — {book} is on your {shelf} shelf now{tail}."
    if action in ("create_shelf", "shelf_exists"):
        return (f"Your {shelf} shelf already exists."
                if action == "shelf_exists" else f"Made a {shelf} shelf.")
    return f"Done — {book} on {shelf}."


def _ask_before_bulk(books: list, shelf: str, verb: str, apply,
                     client_id=None) -> str:
    """A bulk change is a question, not an action (owner policy)."""
    names = [b.title for b in books[:3]]
    sentence = (f"That's {len(books)} books — {_join(names)} and "
                f"{len(books) - len(names)} more. I haven't {verb} anything "
                f"yet. Want me to do all {len(books)}?")

    def _complete(_chosen, said, books=books):
        if not re.match(r"^\s*(yes|yeah|yep|sure|ok|okay|do it|go ahead|please"
                        r"|all of them)\b", said or "", re.IGNORECASE):
            return "Left the shelves alone."
        done = [apply(b) for b in books]
        ok = sum(1 for r in done if r["ok"])
        return f"Done — {ok} of {len(books)} books updated."

    try:
        from puffpai import pending_clarifier
        # The fastpath's client_id IS the conversation id (api_server passes
        # it through), so the "yes" next turn lands on THIS question.
        pending_clarifier.register("books_bulk_shelf", _complete,
                                   conversation_id=client_id or None,
                                   prompt=sentence)
    except Exception as exc:                   # pragma: no cover - never fatal
        logger.debug("bulk shelf confirm not registered: %s", exc)
    return sentence


def do_shelf_add(what: str, shelf: str, client_id=None) -> str:
    books = resolve_reference(what, client_id)
    if not books:
        return miss_reply(f"\"{what}\"", library.nearest_titles(what))
    if not library.shelves_available():
        return SHELVES_UNAVAILABLE
    if len(books) > BULK_LIMIT:
        return _ask_before_bulk(books, shelf, "moved",
                                lambda b: library.add_to_shelf(b.id, shelf),
                                client_id=client_id)
    if len(books) > 1:
        return _bulk_apply(books, lambda b: library.add_to_shelf(b.id, shelf))
    library.remember_last(client_id, books)
    return _one_change(library.add_to_shelf(books[0].id, shelf), "shelve")


def do_shelf_remove(what: str, shelf: str, client_id=None) -> str:
    books = resolve_reference(what, client_id)
    if not books:
        return miss_reply(f"\"{what}\"", library.nearest_titles(what))
    if not library.shelves_available():
        return SHELVES_UNAVAILABLE
    if len(books) > BULK_LIMIT:
        return _ask_before_bulk(books, shelf, "removed",
                                lambda b: library.remove_from_shelf(b.id, shelf),
                                client_id=client_id)
    library.remember_last(client_id, books[:1])
    return _one_change(library.remove_from_shelf(books[0].id, shelf), "remove")


def do_shelf_move(what: str, from_shelf: str, to_shelf: str,
                  client_id=None) -> str:
    books = resolve_reference(what, client_id)
    if not books:
        return miss_reply(f"\"{what}\"", library.nearest_titles(what))
    if not library.shelves_available():
        return SHELVES_UNAVAILABLE
    if len(books) > BULK_LIMIT:
        return _ask_before_bulk(
            books, to_shelf, "moved",
            lambda b: library.move(b.id, from_shelf, to_shelf),
            client_id=client_id)
    library.remember_last(client_id, books[:1])
    return _one_change(library.move(books[0].id, from_shelf, to_shelf), "move")


def do_create_shelf(name: str) -> str:
    if not library.shelves_available():
        return SHELVES_UNAVAILABLE
    return _one_change(library.create_shelf(name), "make that shelf")


def do_show(query: str, client_id=None) -> str | None:
    """"Show me <book>" → the reader on the wall, or None to fall through."""
    payload = show.resolve_book(query, client_id=client_id)
    if not payload:
        return None
    book = library.get(payload["meta"]["book_id"])
    if book is None:                            # pragma: no cover - race guard
        return None
    library.remember_last(client_id, [book])
    return show.show_book(book, source="voice")


# ── the phrasings ───────────────────────────────────────────────────────────

_SHELF_LIST_RE = re.compile(
    r"\b(?:what|which|how many)\s+shelves\b|\b(?:list|name)\s+(?:my|our|the)\s+"
    r"shelves\b|\bmy\s+shelves\b", re.IGNORECASE)
_SHELF_READ_RE = re.compile(
    r"\bwhat(?:'s|s| is| are)?\s+on\s+(?:my|our|the)\s+(?P<name>.+?)"
    r"(?:\s+shelf|\s+list)?\s*[.!?]*$", re.IGNORECASE)
_SHELF_CREATE_RE = re.compile(
    r"\b(?:make|create|start|set up)\s+(?:me\s+)?(?:a\s+|another\s+)?(?:new\s+)?"
    r"shelf\s+(?:called|named|for)?\s*(?P<name>.+?)\s*[.!?]*$", re.IGNORECASE)
_SHELF_ADD_RE = re.compile(
    r"\b(?:put|add|stick|save|file)\s+(?P<what>.+?)\s+(?:on|onto|to|in)\s+"
    r"(?:my|our|the)?\s*(?P<shelf>.+?)(?P<said>\s+shelf)?\s*[.!?]*$",
    re.IGNORECASE)
_SHELF_REMOVE_RE = re.compile(
    r"\b(?:take|remove|pull|get)\s+(?P<what>.+?)\s+"
    r"(?:off(?:\s+of)?|out of|from)\s+(?:my|our|the)?\s*"
    r"(?P<shelf>.+?)(?P<said>\s+shelf)?\s*[.!?]*$", re.IGNORECASE)
_SHELF_MOVE_RE = re.compile(
    r"\bmove\s+(?P<what>.+?)\s+(?:from\s+(?:my|our|the)?\s*(?P<from>.+?)\s+)?"
    r"(?:to|onto|into)\s+(?:my|our|the)?\s*(?P<to>.+?)(?P<said>\s+shelf)?"
    r"\s*[.!?]*$", re.IGNORECASE)

_RECOMMEND_RE = re.compile(
    r"\b(?:recommend|suggest)\b|\bwhat\s+should\s+(?:i|we)\s+read\b"
    r"|\bwhat\s+to\s+read\b|\bgive\s+me\s+something\s+to\s+read\b",
    re.IGNORECASE)
_LIKE_RE = re.compile(
    r"\bsomething\s+(?:like|similar to)\s+(?P<t>.+?)\s*[.!?]*$", re.IGNORECASE)
_NEXT_READ_RE = re.compile(
    r"\bwhat\s+should\s+(?:i|we)\s+read(?:\s+next)?\b", re.IGNORECASE)

_FIND_RE = re.compile(
    r"\b(?:find|get|look for|search for|dig up)\s+(?:me\s+)?"
    r"(?:a\s+|an\s+|some\s+|any\s+)?(?:good\s+)?"
    r"(?P<noun>books?|novels?|comics?|graphic novels?)\b"
    r"(?:\s+(?:about|on|by|in|called|named|titled)\s+(?P<topic>.+?))?"
    r"\s*[.!?]*$", re.IGNORECASE)
_HAVE_BOOKS_RE = re.compile(
    r"\b(?:what|which)\s+(?P<cat>[a-z' &-]*?)\s*"
    r"(?P<noun>books?|comics?|novels?|graphic novels?)\s+"
    r"(?:do|does)\s+(?:we|i|you)\s+have\b", re.IGNORECASE)
_CATEGORY_ASK_RE = re.compile(
    r"\bwhat(?:'s|s| is)?\s+(?:in|on)\s+(?:the\s+|my\s+|our\s+)?(?P<cat>.+?)"
    r"\s+(?:category|section|shelf of books)\b", re.IGNORECASE)
#: "what do I have on survival and preparedness" (BK2). Claimed ONLY when the
#: topic resolves to a real category; otherwise it stays with the pantry's
#: "what do we have ..." read, which runs later and otherwise took this turn.
#: "have on for ..." ("what do we have on for the kids") is a calendar idiom,
#: so a topic starting with "for" is never a library topic.
_HAVE_ON_TOPIC_RE = re.compile(
    r"\bwhat\s+(?:do|does)\s+(?:i|we|you)\s+(?:have|own)\s+(?:on|about)\s+"
    r"(?!for\b)(?P<topic>.+?)\s*[.!?]*$", re.IGNORECASE)
#: Household "on ..." phrasings that are never a book category, even when a
#: category word rides along ("on hand for cooking", "on the calendar").
_HAVE_ON_HOUSE_RE = re.compile(
    r"\b(?:hand|tap|pantry|fridge|freezer|shopping|grocery|groceries|list|"
    r"calendar|schedule|agenda|menu|today|tomorrow|tonight|weekend)\b",
    re.IGNORECASE)
_BY_AUTHOR_RE = re.compile(
    r"\b(?:books?|novels?|comics?)\s+by\s+(?P<a>.+?)\s*[.!?]*$", re.IGNORECASE)
_COMICS_ONLY_RE = re.compile(r"\b(comics?|graphic novels?|manga)\b", re.IGNORECASE)
_SHOW_VERB_RE = re.compile(
    r"\b(show me|show|pull up|bring up|put up|display|open up|open|read me)\b",
    re.IGNORECASE)


def handle(user_message: str, client_id=None) -> str | None:
    """The whole librarian, deterministically. None = not ours, fall through."""
    text = " ".join(str(user_message or "").split())
    low = text.lower()
    if not text or len(text) > 220:
        return None

    # 1. Shelves — the word "shelf" is unambiguous, so these run first.
    m = _SHELF_CREATE_RE.search(text)
    if m:
        name = show._clean_title(m.group("name")) or m.group("name")
        return do_create_shelf(name)
    if _SHELF_LIST_RE.search(low):
        return do_shelves(client_id=client_id)

    m = _SHELF_MOVE_RE.search(text)
    if m and (m.group("said") or _shelfish(m.group("to"), False)):
        if _shelfish(m.group("to"), bool(m.group("said"))):
            return do_shelf_move(m.group("what"), (m.group("from") or "").strip(),
                                 m.group("to").strip(), client_id=client_id)
        return None
    m = _SHELF_ADD_RE.search(text)
    if m and _shelfish(m.group("shelf"), bool(m.group("said"))):
        return do_shelf_add(m.group("what"), m.group("shelf").strip(),
                            client_id=client_id)
    m = _SHELF_REMOVE_RE.search(text)
    if m and _shelfish(m.group("shelf"), bool(m.group("said"))):
        return do_shelf_remove(m.group("what"), m.group("shelf").strip(),
                               client_id=client_id)
    m = _SHELF_READ_RE.search(text)
    if m and _shelfish(m.group("name"), bool(re.search(r"\bshelf\b", low))):
        return do_shelves(m.group("name").strip(), client_id=client_id)

    # 2. Recommendations.
    if _RECOMMEND_RE.search(low):
        m = _LIKE_RE.search(text)
        if m:
            return do_recommend(like=m.group("t"), client_id=client_id)
        cat = ""
        mc = re.search(r"\b(?:recommend|suggest)\s+(?:me\s+)?(?:a|an|some|any)?"
                       r"\s*(?P<c>.+?)\s*[.!?]*$", text, re.IGNORECASE)
        if mc:
            cat = library.resolve_category(mc.group("c")) or ""
        if not cat and not (_NEXT_READ_RE.search(low)
                            or re.search(r"\b(book|books|novel|read|comic|"
                                         r"comics)\b", low)):
            return None          # "recommend a good restaurant" is not ours
        return do_recommend(category=cat, client_id=client_id)
    m = _LIKE_RE.search(text)
    if m and re.search(r"\b(book|novel|comic|read)\b", low):
        return do_recommend(like=m.group("t"), client_id=client_id)

    # 3. "Show me <book>" and the anaphoric follow-ups.
    if _SHOW_VERB_RE.search(low) or show.ANAPHORA_RE.match(text):
        said = do_show(text, client_id=client_id)
        if said:
            return said

    # 4. Finding / browsing.
    m = _CATEGORY_ASK_RE.search(text)
    if m:
        return do_find(category=m.group("cat"), client_id=client_id)
    m = _HAVE_ON_TOPIC_RE.search(text)
    if m and not _HAVE_ON_HOUSE_RE.search(m.group("topic")):
        cat = library.resolve_category(m.group("topic"))
        if cat:
            return do_find(category=cat, client_id=client_id)
    m = _HAVE_BOOKS_RE.search(text)
    if m:
        cat = library.resolve_category(m.group("cat")) if m.group("cat") else ""
        comics = bool(_COMICS_ONLY_RE.search(m.group("noun")))
        if not cat and not comics and m.group("cat").strip():
            return None          # an adjective we don't know — don't guess
        return do_find(category=cat or "", comics_only=comics and not cat,
                       client_id=client_id, limit=10)
    m = _FIND_RE.search(text)
    if m:
        topic = (m.group("topic") or "").strip()
        comics = bool(_COMICS_ONLY_RE.search(m.group("noun")))
        if re.search(r"\bby\s+", text[m.start():], re.IGNORECASE) and topic:
            return do_find(author=topic, comics_only=comics, client_id=client_id)
        cat = library.resolve_category(topic) if topic else ""
        if cat:
            return do_find(category=cat, client_id=client_id)
        if topic:
            return do_find(topic, comics_only=comics, client_id=client_id)
        return do_find(comics_only=comics, client_id=client_id)
    m = _BY_AUTHOR_RE.search(text)
    if m and re.search(r"\b(do|does|any|what|which|got)\b", low):
        return do_find(author=m.group("a").strip(), client_id=client_id)
    return None


__all__ = ["BULK_LIMIT", "parse_args", "parse_flag", "parse_pair", "CATALOGUE_UNAVAILABLE", "PREVIEW",
           "SHELVES_UNAVAILABLE", "do_create_shelf", "do_find", "do_recommend",
           "do_shelf_add", "do_shelf_move", "do_shelf_remove", "do_shelves",
           "do_show", "handle", "list_reply", "miss_reply",
           "resolve_reference"]
