"""«Show me that book» — resolving a spoken title to the reader on the wall.

The movie half of this already exists (``media_library.movie_display_payload``
→ ``/dashboard/media/jellyfin``). A book is the same shape: a SAME-ORIGIN
dashboard route the kiosk overlay can iframe, carrying the Calibre book id and
the format the reader should open.

  ``/dashboard/media/books?book=<calibre id>&fmt=<epub|cbr|cbz|pdf>``

Without ``fmt`` that route opens the book's detail page, which is exactly right
for a MOBI-only book the web reader cannot render.

WHOSE QUERY IS IT. In a 60,000-title library almost any noun is also a book
title — "show me my calendar" found *A Calendar of Sonnets*, "show me the
weather" found *Weather Proverbs* — so a loose title match here HIJACKS the
calendar, the thermostat and the shopping list from the handlers that own them.
Three ways, and only three, a display ask becomes a book:

(a) **Explicit framing** — a book noun (book / novel / comic / issue / graphic
    novel / story / ebook / audiobook), "by <author>", or a series name with an
    issue number.
(b) **An anaphor with a remembered book** — "show it to me", "open that one",
    "the second one".
(c) **A high-confidence WHOLE-title match** — at least two content words,
    normalized equality or similarity ≥ :data:`STRICT_TITLE_MATCH` against the
    entire title (never a substring of a longer one), and not one word from
    :data:`HOUSE_WORDS`, the things the house already answers about.

A possessive before a single noun ("my calendar", "our schedule") is never a
title ask. *movie / film / watch* → the Jellyfin path, and this module declines
outright.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

from puffpai.books import library

logger = logging.getLogger(__name__)

#: The dashboard route that opens a book (reader with ``fmt``, detail page
#: without). The dashboard owns the page; the route is named once, here.
BOOK_VIEWER_ROUTE = "/dashboard/media/books"

BOOK_NOUN_RE = re.compile(
    r"\b(books?|novels?|novella|comics?|comic books?|graphic novels?|"
    r"issues?|e-?books?|audiobooks?|read|reading|paperback|manga|"
    r"short stor(?:y|ies)|stor(?:y|ies))\b",
    re.IGNORECASE)

#: "show me Dracula by Bram Stoker" frames a book as surely as the word does.
BY_AUTHOR_RE = re.compile(r"\bby\s+[A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+)*",
                          re.UNICODE)

#: A whole-title match this good, or the library does not get the turn.
STRICT_TITLE_MATCH = 0.93

#: The house answers about these itself. A title containing one of them is
#: never claimed on a bare-title match — this is the stoplist that keeps
#: "show me my calendar" out of the poetry shelf.
HOUSE_WORDS = frozenset("""
calendar schedule weather forecast thermostat temperature camera cameras
lights light garden kitchen recipe recipes shopping grocery groceries list
lists alarm alarms timer timers settings firewall vault dashboard map maps
news music playlist playlists movie movies tv show shows radio photo photos
picture pictures document documents file files bedroom bathroom living room
basement garage hallway porch office nursery attic yard
""".split())

_WORD_RE = re.compile(r"[a-z0-9']+")
#: "my <one noun>" is a possession, not a title.
_POSSESSIVE_RE = re.compile(r"\b(?:my|our)\s+([a-z]+)\b", re.IGNORECASE)
MOVIE_NOUN_RE = re.compile(
    r"\b(movies?|films?|flicks?|watch|tv show|episode|jellyfin)\b",
    re.IGNORECASE)

#: "show it to me" / "open it" / "pull that up" — no title, a referent.
ANAPHORA_RE = re.compile(
    r"^\s*(?:can you\s+|please\s+)?"
    r"(?:show|pull|bring|put|open|display)\s*"
    r"(?:me\s+)?(?:it|that|this|them|that one|the (?:first|second|third|last) one)"
    r"\s*(?:up|to me|on (?:the )?(?:screen|dashboard|tv|display))?\s*[.!?]*\s*$",
    re.IGNORECASE)

_ORDINAL_RE = re.compile(
    r"\b(first|second|third|fourth|fifth|last)\b", re.IGNORECASE)
_ORDINALS = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4,
             "last": -1}

#: Words that frame a display request but are not part of the title.
_STRIP_RE = re.compile(
    r"\b(show me|show|pull up|pull|bring up|bring|put up|display|open up|open|"
    r"up|me|my|our|the|a|an|please|for me|on (?:the )?(?:screen|dashboard|tv|"
    r"display|kiosk|wall)|in the library|from the library|book|books|novel|"
    r"novels|comic|comics|comic book|graphic novel|ebook|e-book|audiobook|"
    r"to read|read)\b", re.IGNORECASE)

_ISSUE_OF_RE = re.compile(
    r"\b(?:issue|number|no\.?|#|volume|vol\.?|part)\s*(\d+)\s+of\s+(.+)$",
    re.IGNORECASE)
_ISSUE_TRAIL_RE = re.compile(
    r"^(.+?)\s+(?:issue|number|no\.?|#|volume|vol\.?|part)\s*(\d+)\s*$",
    re.IGNORECASE)


def is_book_ask(text) -> bool:
    """True when the words name a BOOK and not a movie — the tie-breaker the
    movie fastpath consults before it claims a bare title."""
    low = str(text or "")
    return bool(BOOK_NOUN_RE.search(low)) and not MOVIE_NOUN_RE.search(low)


def names_a_house_thing(text) -> bool:
    """True when the utterance names something the house already answers about
    (the calendar, the weather, a room, the shopping list)."""
    return any(w in HOUSE_WORDS for w in _WORD_RE.findall(str(text or "").lower()))


def _possessive_single_noun(text) -> bool:
    """"show me my calendar" — a possessive in front of ONE noun is a thing the
    household owns, never a title."""
    m = _POSSESSIVE_RE.search(str(text or ""))
    if not m:
        return False
    tail = str(text or "")[m.end():]
    return not _WORD_RE.findall(tail.lower())


def _content_words(title: str) -> list:
    return [w for w in _WORD_RE.findall(str(title or "").lower()) if len(w) > 1]


_SUBTITLE_RE = re.compile(r"\s*[;:(\[].*$")


def _title_forms(title: str) -> list:
    """A catalogue title and its main part: "Moby Dick; Or, The Whale" is named
    by "Moby Dick". A SUBTITLE is not the same thing as a longer title — the
    rule that must stay broken is "Dracula" naming "Dracula Redux"."""
    forms = [str(title or "")]
    main = _SUBTITLE_RE.sub("", forms[0]).strip()
    if main and main != forms[0]:
        forms.append(main)
    return forms


def _whole_title_match(title: str):
    """Case (c): the cleaned words ARE a whole title in the catalogue."""
    from puffpai.media_library import normalize_title, title_similarity
    want = normalize_title(title)
    if not want:
        return None
    for book in library.find(title, limit=10):
        for form in _title_forms(book.title):
            if (normalize_title(form) == want
                    or title_similarity(title, form) >= STRICT_TITLE_MATCH):
                return book
    return None


_BY_SPLIT_RE = re.compile(r"\s+by\s+", re.IGNORECASE)


def _split_by_author(title: str) -> tuple:
    """``"Frankenstein by Mary Shelley"`` → ``("Frankenstein", "Mary Shelley")``."""
    parts = _BY_SPLIT_RE.split(str(title or ""), maxsplit=1)
    if len(parts) == 2 and parts[0].strip():
        return parts[0].strip(), parts[1].strip()
    return str(title or "").strip(), ""


def _clean_title(text) -> str:
    out = _STRIP_RE.sub(" ", str(text or ""))
    out = re.sub(r"[\"“”']", " ", out)
    return " ".join(out.split()).strip(" .,?!-")


def _series_issue(text):
    """("Ultimate X-Men", 54) for "issue 54 of Ultimate X-Men", else None."""
    for rx, order in ((_ISSUE_OF_RE, "num_first"), (_ISSUE_TRAIL_RE, "name_first")):
        m = rx.search(str(text or ""))
        if not m:
            continue
        if order == "num_first":
            number, name = m.group(1), m.group(2)
        else:
            name, number = m.group(1), m.group(2)
        name = _clean_title(name)
        if name:
            try:
                return name, int(number)
            except ValueError:
                return None
    return None


def find_issue(series: str, index: int):
    """One numbered issue of a comic series, from the real catalogue."""
    for book in library.find(series=series, limit=library.POOL_LIMIT):
        if book.series_index is not None and int(book.series_index) == int(index):
            return book
    return None


def book_display_payload(book) -> dict:
    """The display dict for one resolved book (same-origin, KIND_URL)."""
    from puffpai import dashboard_display as dd
    url = f"{BOOK_VIEWER_ROUTE}?book={quote(str(book.id))}"
    fmt = book.best_format
    if fmt:
        url += f"&fmt={fmt.lower()}"
    return {
        "kind": dd.KIND_URL,
        "url": url,
        "title": book.title,
        "meta": {
            "source": "calibre",
            "media_type": "comic" if book.is_comic else "book",
            "book_id": book.id,
            "author": book.author,
            "series": book.series,
            "series_index": book.series_index,
            "format": fmt,
        },
    }


def resolve_book(query: str, client_id=None) -> dict | None:
    """A "show me …" ask → the book display payload, or None to fall through.

    Declines (None) for a movie-framed ask, for anything the catalogue does not
    actually hold, and — with no book noun anywhere in the query — for anything
    that is not a CONFIDENT title match, so a topic question still reaches the
    knowledge shelves.
    """
    text = str(query or "").strip()
    if not text or MOVIE_NOUN_RE.search(text):
        return None
    framed = bool(BOOK_NOUN_RE.search(text)) or bool(BY_AUTHOR_RE.search(text))

    # (b) "show it to me" — the book we just named.
    if ANAPHORA_RE.match(text):
        remembered = library.last(client_id)
        if not remembered:
            return None
        idx = 0
        m = _ORDINAL_RE.search(text)
        if m:
            idx = _ORDINALS.get(m.group(1).lower(), 0)
        try:
            book = remembered[idx]
        except IndexError:
            book = remembered[0]
        return book_display_payload(book)

    if not library.catalogue_available():
        return None

    # (a) a series and an issue number frame a comic all by themselves.
    issue = _series_issue(text)
    if issue:
        book = find_issue(*issue)
        if book is not None:
            return book_display_payload(book)

    # A possessive in front of one noun is a thing, not a title — and a house
    # word anywhere means the handler that owns it should get this turn.
    if _possessive_single_noun(text):
        return None
    title = _clean_title(text)
    if len(title) < 3:
        return None

    if framed:
        if names_a_house_thing(title) and not BOOK_NOUN_RE.search(title):
            # "show me my recipe book" — the uploads/recipe path owns it.
            return None
        # "Frankenstein by Mary Shelley": the words after "by" are the AUTHOR,
        # and searching them as part of the title finds nothing.
        named_title, named_author = _split_by_author(title)
        book = library.best_match(named_title)
        if book is None:
            hits = library.find(named_title, author=named_author, limit=1)
            book = hits[0] if hits else None
    else:
        # (c) an unframed ask has to BE a whole title, and must not name
        # anything the house answers about.
        # The stoplist is checked against the CLEANED words: every one of these
        # asks opens with "show me", and "show" is itself a house word.
        if names_a_house_thing(title) or len(_content_words(title)) < 2:
            return None
        book = _whole_title_match(title)
    if book is None:
        return None
    return book_display_payload(book)


def show_book(book, source: str = "voice") -> str:
    """Queue the viewer payload for *book* and return the spoken confirmation."""
    from puffpai import dashboard_display as dd
    payload = book_display_payload(book)
    dd.set_display(payload["kind"], payload["url"], payload["title"],
                   source=source, meta=payload["meta"])
    return f"Pulling up {book.label} from your library."


__all__ = ["BOOK_VIEWER_ROUTE", "BOOK_NOUN_RE", "BY_AUTHOR_RE", "HOUSE_WORDS",
           "MOVIE_NOUN_RE", "STRICT_TITLE_MATCH", "book_display_payload",
           "find_issue", "is_book_ask", "names_a_house_thing", "resolve_book",
           "show_book"]
