"""FitFam entry deletion + "whose workout?" resolution.

A mis-logged FitFam entry (wrong person, wrong type) must be removable. This
module is the deterministic half of that: it parses the declared delete/undo
phrasings and turns them into exactly one ``AppData.fit_delete_entry`` call.

Two rules shape everything here:

* **Never guess whose slice.** FitFam data is per-user inside one household
  file. When the phrasing does not name a person and the recent-write heuristic
  cannot pick one, we ASK ("whose workout?") rather than defaulting to the first
  user in the file. (Household-wide reads stay allowed; this gate is about
  picking a slice to WRITE to.)
* **Confirm by title only.** Replies name the workout title and nothing else —
  no duration, no date, no streak, no age. FitFam entries are health data
  (and a child user is permitted), so the confirmation text is deliberately
  value-free.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta

# How recently a workout must have been logged for a bare "undo that" to be
# read as "undo the workout I just logged". Outside this window the phrase is
# left alone entirely (returns None) so it cannot hijack another app's undo.
RECENT_WINDOW = timedelta(minutes=15)

# How long a pending "whose workout?" / "which one?" question stays answerable.
PENDING_TTL = timedelta(minutes=5)

_DELETE_VERB_RE = re.compile(
    r"(?i)\b(?:delete|remove|undo|erase|scratch|take\s+back|get\s+rid\s+of)\b")

# The workout noun that makes a delete unambiguously FitFam's.
_WORKOUT_NOUN_RE = re.compile(
    r"(?i)\b(?:workouts?|sessions?|trainings?|exercises?|lifts?)\b")

# "the last one", "the one I just logged", "my latest".
_LAST_RE = re.compile(
    r"(?i)\b(?:last|latest|most\s+recent|previous)\b"
    r"|\bi\s+just\s+(?:logged|did|recorded|added)\b"
    r"|\bjust\s+logged\b|\bthat\b|\bit\b")

# Bare "undo that" / "undo it" / "undo the last one" — no workout noun at all.
_BARE_UNDO_RE = re.compile(
    r"(?i)^\s*(?:undo|scratch)\s+(?:that|it|the\s+last(?:\s+one)?)\s*[.!]?\s*$")

# Nouns owned by other apps. Their presence means this is not a FitFam delete,
# even when a workout title happens to be a substring of the sentence.
_OTHER_APP_RE = re.compile(
    r"(?i)\b(?:alarms?|timers?|tasks?|to-?dos?|events?|appointments?|"
    r"reminders?|recipes?|meals?|ingredients?|pantry|shopping|groceries|"
    r"medications?|meds?|bills?|widgets?|dashboards?|documents?|files?|"
    r"photos?|messages?|playlists?|stations?|contacts?)\b")

# "for <name>" / "<name>'s" / "mine".
_FOR_USER_RE = re.compile(r"(?i)\bfor\s+([a-z][a-z0-9_-]{1,20})\b")
_POSSESSIVE_USER_RE = re.compile(r"(?i)\b([a-z][a-z0-9_-]{1,20})'s\b")

_pending_lock = threading.Lock()
_pending: "dict | None" = None


def _norm(text) -> str:
    """Fold a title to comparable form: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def norm_title(text) -> str:
    """Public spelling of :func:`_norm`, for callers comparing two titles."""
    return _norm(text)


def resolve_user(users: dict, requested) -> "tuple[str | None, list]":
    """Map *requested* onto one FitFam user key.

    Matches the slice key or the stored display name, case-insensitively, then
    falls back to a unique prefix match. Returns ``(key, candidates)``; ``key``
    is None when nothing (or more than one thing) matched, and ``candidates``
    then holds the keys that did.
    """
    want = _norm(requested)
    if not want or not isinstance(users, dict):
        return None, []
    for key, u in users.items():
        if want in (_norm(key), _norm((u or {}).get('name'))):
            return key, [key]
    hits = [key for key, u in users.items()
            if _norm(key).startswith(want)
            or _norm((u or {}).get('name')).startswith(want)]
    if len(hits) == 1:
        return hits[0], hits
    return None, hits


def _entry_title(entry) -> str:
    return str((entry or {}).get('type') or '').strip()


def _entry_when(entry):
    try:
        return datetime.fromisoformat(str((entry or {}).get('date') or ''))
    except (TypeError, ValueError):
        return None


def find_by_title(history, title, strict: bool = False) -> list:
    """Indices in *history* whose title fuzzy-matches *title*.

    Fuzzy means: normalized substring in either direction, so "test
    fakeworkout" finds "test-fakeworkout" and "run" finds "trail run".

    ``strict`` drops the reverse direction (a stored title being a substring of
    what was said). It is used for sentences that carry NO workout noun, where
    the title is the only proof this was a FitFam delete at all — without it,
    "remove the running shoes from the list" would match a "running" entry.
    """
    want = _norm(title)
    if not want or len(want) < 3:
        return []
    out = []
    for i, entry in enumerate(history or []):
        have = _norm(_entry_title(entry))
        if not have:
            continue
        if want in have or (not strict and have in want):
            out.append(i)
    return out


def recent_logger(users: dict, now=None) -> "str | None":
    """The one user with a workout logged inside :data:`RECENT_WINDOW`.

    None when nobody logged recently, or when more than one person did — in
    which case the caller must ask rather than guess.
    """
    now = now or datetime.now()
    hits = []
    for key, u in (users or {}).items():
        for entry in (u or {}).get('history', []):
            when = _entry_when(entry)
            if when is not None and now - when <= RECENT_WINDOW:
                hits.append(key)
                break
    return hits[0] if len(hits) == 1 else None


def parse_delete(text: str) -> "dict | None":
    """Read a delete/undo intent out of *text*, or None to fall through.

    Returns ``{'scope': 'last'|'title', 'title': str|None, 'user': str|None,
    'bare_undo': bool, 'needs_title_proof': bool}``. Deliberately conservative:
    a delete verb alone is never enough — the sentence must also carry a
    workout noun, or be the bare "undo that" shape, or name an entry title.
    That last case sets ``needs_title_proof``: the title is the only evidence
    the sentence is about FitFam at all, so the caller must find it in real
    history (with ``find_by_title(..., strict=True)``) before acting, and fall
    through untouched when it does not.
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 160:
        return None
    if not _DELETE_VERB_RE.search(raw):
        return None
    if _OTHER_APP_RE.search(raw):
        return None

    bare = bool(_BARE_UNDO_RE.match(raw))
    has_noun = bool(_WORKOUT_NOUN_RE.search(raw))
    user = _parse_user(raw)
    if not bare and not has_noun:
        # Might still be "delete <title>". The title is then the ONLY evidence
        # this is a FitFam delete, so the caller must prove it against the
        # user's real history with a strict match.
        title = _leftover_title(raw, user)
        if not title or len(title.split()) > 4:
            return None
        return {'scope': 'title', 'title': title, 'user': user,
                'bare_undo': False, 'needs_title_proof': True}

    if bare:
        return {'scope': 'last', 'title': None, 'user': user,
                'bare_undo': True, 'needs_title_proof': False}
    if _LAST_RE.search(raw):
        return {'scope': 'last', 'title': None, 'user': user,
                'bare_undo': False, 'needs_title_proof': False}
    title = _leftover_title(raw, user)
    if title:
        return {'scope': 'title', 'title': title, 'user': user,
                'bare_undo': False, 'needs_title_proof': False}
    return {'scope': 'last', 'title': None, 'user': user,
            'bare_undo': False, 'needs_title_proof': False}


_STOPWORDS = frozenset("""
delete remove undo erase scratch take back get rid of the a an my mine our
your his her their that this it one last latest most recent previous just
logged log record recorded added workout workouts session sessions training
trainings exercise exercises lift lifts entry entries from for please can you
i we today yesterday s
""".split())


def _leftover_title(raw: str, user=None) -> "str | None":
    """The words left after the command scaffolding — the candidate title.

    *user*, when the sentence named one, is dropped: "delete the test-fake
    workout for <name>" is about a title, and the name is not part of it.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9-]+", raw) if w]
    if user:
        words = [w for w in words if w.lower() != str(user).lower()]
    kept = [w for w in words if w.lower() not in _STOPWORDS]
    title = " ".join(kept).strip()
    return title or None


def _parse_user(raw: str) -> "str | None":
    m = _FOR_USER_RE.search(raw)
    if m and m.group(1).lower() not in _STOPWORDS:
        return m.group(1)
    m = _POSSESSIVE_USER_RE.search(raw)
    if m and m.group(1).lower() not in _STOPWORDS:
        return m.group(1)
    return None


# ── pending question state ───────────────────────────────────────────────────
# One slot, not a map: this is a single household appliance and the question is
# always about the turn that just happened. Expires so a stale answer can never
# delete something minutes later.

def set_pending(kind: str, **fields) -> None:
    global _pending
    with _pending_lock:
        _pending = dict(fields, kind=kind, ts=datetime.now())


def peek_pending() -> "dict | None":
    """The pending question if it has not expired, WITHOUT consuming it."""
    with _pending_lock:
        p = _pending
    if not p or datetime.now() - p['ts'] > PENDING_TTL:
        return None
    return p


def take_pending() -> "dict | None":
    """Pop the pending question if it has not expired."""
    global _pending
    with _pending_lock:
        p = _pending
        _pending = None
    if not p:
        return None
    if datetime.now() - p['ts'] > PENDING_TTL:
        return None
    return p


def clear_pending() -> None:
    global _pending
    with _pending_lock:
        _pending = None


def whose_workout(user_keys) -> str:
    """The ask. Names the slices and nothing about their contents."""
    names = ", ".join(sorted(user_keys or []))
    if not names:
        return "There are no FitFam profiles set up yet, so there is nothing to delete."
    return f"Whose workout? Say {names}."


# ── TODO · streak nudges → generic scheduler ────────────────────────────────
# Expose FitFam streak reminders as `schedulable` items for the generic
# scheduler. NOT IMPLEMENTED, neither half of the contract exists yet:
#   * apps/fitfam/app.json (puffpai_app/v1) declares no `reminders` block; its
#     only declared surface is `daily.fitness` and its actions are
#     log_workout / show_streak.
#   * the scheduler publishes no `schedulable` interface for apps.
# Shape to implement once BOTH land — a manifest `reminders: [{id, says, when}]`
# would map to one schedulable descriptor per user slice:
#     {"id": "fitfam.streak.<user>", "app": "fitfam", "owner": "<user key>",
#      "when": "<cron or daily HH:MM from the manifest>",
#      "says": "<manifest text>", "skip_if": "logged_today"}
# with `skip_if` evaluated against that user's own history so a nudge never
# fires on a day they already logged, and the descriptor carrying the user key
# so the scheduler never has to guess a slice (same rule as above).
