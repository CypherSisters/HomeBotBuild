"""
PuffPAI Media Library — title-first lookups over the REAL library.

WHY THIS EXISTS. Left to the generic routes, a library question that names a
*title* or an *author* is answered by RAG instead of by the library:

  * "show me Casablanca" → a Wikipedia plot summary, no viewer payload,
    Jellyfin never touched (the movie is on disk).
  * "show me the movie Casablanca on the tv" → an unrelated dashboard
    widget, because the leftover topic goes to the ZIM shelves.
  * "do we have Back to the Future" → an answer made up from an unrelated
    uploaded document, while the SAME library answers "do we have any horror movies"
    correctly.
  * "do we have any Jane Austen books" → invented library-lending details;
    the real Calibre catalogue is never queried.
  * "what playlists do I have saved" → the entire Home Assistant device
    inventory.

In every case the data is there and the ROUTING is wrong. This module is the
one place that answers a library question, so the api_server fastpaths stay
thin and every answer is grounded in a real Jellyfin/Calibre response. The
cardinal rule here: **never answer from the model.** If the library cannot be
reached we say so; if a title is not in it we say that and offer the closest
things we actually have.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import difflib
import logging
import re

logger = logging.getLogger(__name__)

# A confident title match. 0.86 keeps "back to the future" ↔ "Back to the
# Future Part II" (ratio ≈ 0.82) apart while still absorbing punctuation/article/STT drift.
TITLE_MATCH_THRESHOLD = 0.86

# How many "closest thing I actually have" suggestions to offer on a miss.
SUGGEST_LIMIT = 3

_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_AMP_RE = re.compile(r"\s*&\s*")


def normalize_title(text) -> str:
    """Fold a spoken/typed title to a comparable key.

    Lowercase, "&"→"and", punctuation dropped, leading article dropped,
    whitespace collapsed. "The Dark Tower!" and "dark tower" fold together;
    "Back to the Future" and "Back to the Future Part II" do not.
    """
    low = _AMP_RE.sub(" and ", str(text or "").lower())
    low = _PUNCT_RE.sub(" ", low)
    low = " ".join(low.split())
    return _LEADING_ARTICLE_RE.sub("", low).strip()


def title_similarity(a, b) -> float:
    """0..1 similarity of two titles after normalisation."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _get_bridge(bridge=None):
    if bridge is not None:
        return bridge
    from puffpai.media_bridge import get_media_bridge
    return get_media_bridge()


def _title_of(item) -> str:
    return str(getattr(item, "title", "") or "")


def _year_of(item) -> int:
    try:
        return int(getattr(item, "year", 0) or 0)
    except (TypeError, ValueError):
        return 0


def label_movie(item) -> str:
    """"Casablanca (1942)" — the spoken/printed label for one movie."""
    title = _title_of(item)
    year = _year_of(item)
    return f"{title} ({year})" if year else title


# ── Movies (Jellyfin) ────────────────────────────────────────────────────────

def find_movie(query: str, bridge=None) -> dict:
    """Resolve a spoken movie title against the REAL Jellyfin library.

    Returns ``{"status": …, "match": item|None, "candidates": [item, …]}``
    where status is one of:

      ``"match"``       — a confident title hit (``match`` is the item)
      ``"no_match"``    — the library answered, nothing matched (``candidates``
                          holds the closest titles we DO have)
      ``"unavailable"`` — Jellyfin could not be reached / is disabled

    Two passes, because Jellyfin's ``SearchTerm`` is exact-ish: the search API
    first (cheap, usually enough), then a full title list ranked by fuzzy
    similarity — which is also where the "closest 3" suggestions come from.
    """
    from puffpai.media_bridge import MediaBridgeError
    q = (query or "").strip()
    out: dict = {"status": "no_match", "match": None, "candidates": []}
    if not q:
        return out
    br = _get_bridge(bridge)

    hits: list = []
    try:
        hits = list(br.search_movies(q, limit=20) or [])
    except MediaBridgeError as exc:
        logger.info("movie search unavailable: %s", exc)
        out["status"] = "unavailable"
        return out
    except Exception as exc:            # a bridge that cannot even be built
        logger.warning("movie search failed: %s", exc)
        out["status"] = "unavailable"
        return out

    best = _best_by_similarity(q, hits)
    if best and best[1] >= TITLE_MATCH_THRESHOLD:
        out["status"] = "match"
        out["match"] = best[0]
        return out

    # Second pass: the whole movie list, for both a fuzzy hit the search API
    # missed and the "closest thing I have" suggestions on a miss.
    try:
        everything = list(br.search_movies("", limit=500) or [])
    except MediaBridgeError as exc:
        logger.info("movie listing unavailable: %s", exc)
        if not hits:
            out["status"] = "unavailable"
        return out
    except Exception as exc:
        logger.warning("movie listing failed: %s", exc)
        if not hits:
            out["status"] = "unavailable"
        return out

    if not everything and not hits:
        # An empty library and an unreachable one look alike from here; the
        # connection probe tells them apart so we never claim "not in your
        # library" about a server we could not ask.
        try:
            if not br.is_connected():
                out["status"] = "unavailable"
                return out
        except Exception:
            out["status"] = "unavailable"
            return out

    ranked = sorted(
        ((it, title_similarity(q, _title_of(it))) for it in everything),
        key=lambda pair: pair[1], reverse=True,
    )
    if ranked and ranked[0][1] >= TITLE_MATCH_THRESHOLD:
        out["status"] = "match"
        out["match"] = ranked[0][0]
        return out
    out["candidates"] = [it for it, score in ranked[:SUGGEST_LIMIT] if score > 0]
    return out


def _best_by_similarity(query: str, items) -> tuple | None:
    best = None
    for it in items or []:
        score = title_similarity(query, _title_of(it))
        if best is None or score > best[1]:
            best = (it, score)
    return best


def movie_not_found_reply(query: str, candidates) -> str:
    """The honest miss: name what was asked for, then the closest real titles.

    Never a plot summary, never a guess — this is the sentence that replaces
    RAG trivia about a title the library does not hold.
    """
    msg = f"I don't have {query} in the library."
    names = [label_movie(c) for c in (candidates or [])][:SUGGEST_LIMIT]
    if names:
        if len(names) == 1:
            msg += f" The closest I have is {names[0]}."
        else:
            msg += (" The closest I have are "
                    + ", ".join(names[:-1]) + f" and {names[-1]}.")
    return msg


LIBRARY_UNAVAILABLE_MSG = (
    "I can't reach your media server right now, so I can't tell you what's in "
    "the movie library.")


# ── The show-me viewer payload ───────────────────────────────────────────────
#
# The kiosk overlay (dashboard ShowViewer) iframes a SAME-ORIGIN dashboard path
# for a non-vault item — an off-site URL is rejected by dashboard_display.
# set_display, and the Jellyfin origin (:8096) is off-site. So a library title
# is pulled up as the dashboard's own Jellyfin page, and the resolved item is
# carried in the query string AND in ``meta`` so the player can deep-link to it
# (frontend follow-up; today the overlay opens the library player itself).

MOVIE_VIEWER_ROUTE = "/dashboard/media/jellyfin"


def movie_display_payload(item) -> dict:
    """The display dict for one resolved library movie (same-origin, KIND_URL)."""
    from urllib.parse import quote
    from puffpai import dashboard_display as dd
    item_id = str(getattr(item, "item_id", "") or "")
    title = _title_of(item)
    url = MOVIE_VIEWER_ROUTE
    if item_id:
        url += f"?item={quote(item_id)}"
    return {
        "kind": dd.KIND_URL,
        "url": url,
        "title": label_movie(item),
        "meta": {
            "source": "jellyfin",
            "media_type": "movie",
            "item_id": item_id,
            "movie_title": title,
            "year": _year_of(item) or None,
            "genres": str(getattr(item, "genre", "") or ""),
        },
    }


def show_movie(item, source: str = "voice", display=None) -> str:
    """Queue the viewer payload for *item* and return the spoken confirmation."""
    if display is None:
        from puffpai import dashboard_display as display
    payload = movie_display_payload(item)
    display.set_display(payload["kind"], payload["url"], payload["title"],
                        source=source, meta=payload["meta"])
    return f"Pulling up {label_movie(item)} from your library."


def movie_present_reply(item) -> str:
    """"Do we have X" → the grounded yes."""
    genres = str(getattr(item, "genre", "") or "").strip()
    tail = f" It's filed under {genres}." if genres else ""
    return (f"Yes — {label_movie(item)} is in your movie library.{tail} "
            f"Say \"show me {_title_of(item)}\" and I'll pull it up.")


# ── Books (Calibre) ──────────────────────────────────────────────────────────

BOOKS_UNAVAILABLE_MSG = (
    "I can't reach your Calibre book catalogue right now, so I can't tell you "
    "what's on the shelf.")


def books_available(bridge=None) -> bool:
    """True when the real Calibre database can be opened."""
    br = _get_bridge(bridge)
    try:
        return bool(br.books_available())
    except Exception:
        return False


def find_books(query: str = "", author: str = "", limit: int = 20,
               bridge=None) -> dict:
    """Search the REAL Calibre catalogue by author and/or title.

    Returns ``{"status": "ok"|"unavailable", "items": [...]}``. An author query
    is tried as an author FIRST (that is what "any Jane Austen books"
    means) and falls back to a title match, so a name that is really a series
    title still lands.
    """
    br = _get_bridge(bridge)
    if not books_available(br):
        return {"status": "unavailable", "items": []}
    items: list = []
    try:
        if author:
            items = list(br.search_books("", author=author, limit=limit) or [])
        if not items and (query or author):
            items = list(br.search_books(query or author, limit=limit) or [])
    except Exception as exc:
        logger.warning("calibre search failed: %s", exc)
        return {"status": "unavailable", "items": []}
    return {"status": "ok", "items": items}


def books_reply(who: str, items, by: bool = True) -> str:
    """Grounded answer for a book question. Only real catalogue rows.

    ``by`` False phrases *who* as a search phrase rather than an author, so
    "any horror books" does not come back as "anything by horror".
    """
    if not items:
        if not who:
            return "I couldn't find that in your book catalogue."
        return (f"I don't have anything by {who} in your book catalogue." if by
                else f"I don't have anything matching \"{who}\" in your book catalogue.")
    lines = []
    for it in items[:8]:
        title = _title_of(it)
        by = str(getattr(it, "artist", "") or "").strip()
        lines.append(f"{title} by {by}" if by else title)
    if who and by:
        head = f"Yes — I found {len(items)} in your book catalogue by {who}: "
    elif who:
        head = f"I found {len(items)} in your book catalogue matching \"{who}\": "
    else:
        head = f"I found {len(items)} in your book catalogue: "
    more = "" if len(items) <= 8 else f" …and {len(items) - 8} more."
    return head + "; ".join(lines) + "." + more


# ── Playlists (Jellyfin + Spotify) ───────────────────────────────────────────

def collect_playlists(bridge=None, spotify=None) -> dict:
    """The REAL saved playlists, per source.

    Returns ``{"jellyfin": [names], "spotify": [names], "errors": [source, …]}``.
    A source that cannot be reached lands in ``errors`` — it is never silently
    reported as "none saved".
    """
    out: dict = {"jellyfin": [], "spotify": [], "errors": []}
    br = None
    try:
        br = _get_bridge(bridge)
    except Exception as exc:
        logger.info("media bridge unavailable for playlists: %s", exc)
        out["errors"].append("jellyfin")
    if br is not None:
        try:
            for pl in br.list_playlists() or []:
                name = _title_of(pl)
                count = 0
                try:
                    count = int((getattr(pl, "extra", {}) or {}).get("track_count") or 0)
                except (TypeError, ValueError):
                    count = 0
                out["jellyfin"].append(f"{name} ({count} tracks)" if count else name)
        except Exception as exc:
            logger.info("jellyfin playlists unavailable: %s", exc)
            out["errors"].append("jellyfin")

    ctl = spotify
    if ctl is None:
        try:
            from puffpai.spotify_control import get_control
            ctl = get_control()
        except Exception:
            ctl = None
    if ctl is not None:
        try:
            if ctl.is_configured():
                for pl in ctl._my_playlists(pages=1) or []:
                    name = str((pl or {}).get("name") or "").strip()
                    if name:
                        out["spotify"].append(name)
        except Exception as exc:
            logger.info("spotify playlists unavailable: %s", exc)
            out["errors"].append("spotify")
    return out


#: How many playlist names a single spoken answer reads out. A cap is right —
#: 25 names is not an answer, it is a recital — but it must be DECLARED.
PLAYLIST_PREVIEW = 12


def _playlist_sentence(label: str, names: list) -> str:
    """One source's playlists, with the truncation stated out loud.

    Reading out the first dozen of 25 playlists alphabetically, stopping at
    "N" with no notice, leaves the owner no way to know 13 more exist.
    Silently returning half an
    inventory is the same defect class as a fabricated one: the user believes
    something about their house that is not true.
    """
    shown = names[:PLAYLIST_PREVIEW]
    body = ", ".join(shown)
    hidden = len(names) - len(shown)
    if hidden > 0:
        return (f"{label} (showing {len(shown)} of {len(names)}): {body}, "
                f"and {hidden} more.")
    return f"{label}: {body}."


def playlists_reply(found: dict) -> str:
    """Spoken answer for "what playlists do I have saved"."""
    jf = found.get("jellyfin") or []
    sp = found.get("spotify") or []
    errors = found.get("errors") or []
    parts = []
    if jf:
        parts.append(_playlist_sentence("On your media server", jf))
    if sp:
        parts.append(_playlist_sentence("On Spotify", sp))
    if parts:
        return "Here are your saved playlists. " + " ".join(parts)
    if errors:
        which = " and ".join(sorted({"Spotify" if e == "spotify" else "your media server"
                                     for e in errors}))
        return (f"I couldn't reach {which} just now, so I can't list your "
                f"playlists. Nothing else reported any saved playlists.")
    return "You don't have any playlists saved yet — not on your media server, not on Spotify."


__all__ = [
    "normalize_title", "title_similarity", "label_movie",
    "find_movie", "movie_not_found_reply", "movie_present_reply",
    "movie_display_payload", "show_movie", "MOVIE_VIEWER_ROUTE",
    "LIBRARY_UNAVAILABLE_MSG", "TITLE_MATCH_THRESHOLD",
    "books_available", "find_books", "books_reply", "BOOKS_UNAVAILABLE_MSG",
    "collect_playlists", "playlists_reply", "PLAYLIST_PREVIEW",
]
