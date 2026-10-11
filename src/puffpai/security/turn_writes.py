"""What THIS chat turn actually STORED — the record the honesty notice consults.

WHY THIS EXISTS
---------------
``api_server._apply_pii_honesty`` exists to stop PuffPAI claiming it kept
something it threw away (*"your SSN has been noted"* when nothing was
vaulted). The notice it prepends — *"I spotted <X> and did not keep it"* — is
only true when nothing on the turn stored <X>.

For an SSN that is always true: no app on the appliance has a place to put one.
For a **medication** it is the opposite. The calendar KEEPS a medication
list, and the deterministic repair path
(``api_server._fix_add_med`` -> ``AppData.cal_add_med``) writes it to
``calendar.json`` on exactly the turns the free-text health detector fires on.
A notice generated from the detector alone would therefore tell the household
their medication was "not saved anywhere" while it sat in ``calendar.meds[]``.

That is the same false claim pointed the other way, and it is worse:
a false "we kept it" costs trust, a false "we did not keep it" costs a person
their medication reminder because they believe they have to say it again.

WHAT THIS MODULE IS
-------------------
A per-thread note of the app-data writes a served turn made, expressed in the
SAME class vocabulary the honesty notice speaks (``app_data_tags``: only
``health.condition``, ``health.device_reading``, ``financial.account_balance``
and ``child.any`` can ever appear). Nothing else. It records **classes and
schema paths — never a value**, exactly like the tagger it borrows.

The recording window is opened by ``api_server._prepare_user_message`` (all four
chat surfaces) and by the voice route, and the hook is ``AppData._save`` — the
one place any app document reaches disk — so every write path is covered: the
REST PUT, the action envelope, and the ``_fix_*`` direct calls that the PUT and
action taggers never see.

TWO DELIBERATE ROUNDINGS
------------------------
1. **Whole-document, not delta.** ``_save`` is handed the finished document, not
   a diff, and reading the previous copy back to difference it would double
   every app write's disk cost on the request path. So a class counts as
   "stored this turn" when a write landed on a document that HOLDS at least one
   entry of it. The error direction is one-way and chosen: the worst this can do
   is keep PuffPAI **silent** about something it really did discard. It can
   never make PuffPAI claim a keep that did not happen — that claim is not this
   module's to make.
2. **Thread-local, not global.** The alarm scheduler and the sync poller write
   app data on their own threads; a global set would let a background alarm save
   silence a chat turn's honest notice. A served turn only ever sees its own
   writes.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import threading

_state = threading.local()


def begin_turn() -> None:
    """Open (or reopen) the recording window on the calling thread.

    Called once per served chat/voice turn, BEFORE any routing runs. Resetting
    here rather than at the end of a turn is what makes the window safe on a
    reused worker thread: whatever the last request left behind is discarded
    before this turn records anything.
    """
    _state.active = True
    _state.classes = set()
    _state.paths = []


def end_turn() -> None:
    """Close the recording window on this thread.

    Not required for correctness in the request path — ``begin_turn`` discards
    whatever the previous request left — but it keeps a reused worker thread
    from tagging app-data writes made OUTSIDE a served turn (a REST PUT that
    lands on the same thread after a chat turn), and it is what test files use
    so a turn opened in one test cannot silence an honesty notice in the next.
    """
    _state.active = False
    _state.classes = set()
    _state.paths = []


def turn_active() -> bool:
    """True while a served turn is recording on this thread."""
    return bool(getattr(_state, "active", False))


def note_write(app, document) -> None:
    """Record an app-data write. Never raises, never inspects a value.

    A no-op outside a turn, which keeps the tagging cost off the background
    writers entirely. Failure is silent by design: this is an honesty aid, and
    a bug in it must never be the reason a household loses a calendar entry.
    """
    if not turn_active():
        return
    try:
        from puffpai.security import app_data_tags
        for tag in app_data_tags.tag_document(app, document):
            if tag.get("count"):
                _state.classes.add(str(tag["class"]))
                _state.paths.append(str(tag["path"]))
    except Exception:
        return


def stored_classes() -> frozenset:
    """The data classes this turn's writes landed in. Empty outside a turn."""
    return frozenset(getattr(_state, "classes", None) or ())


def stored_paths() -> tuple:
    """Schema paths written this turn, in order — for the reply's "and where"."""
    return tuple(getattr(_state, "paths", None) or ())


__all__ = [
    "begin_turn",
    "end_turn",
    "turn_active",
    "note_write",
    "stored_classes",
    "stored_paths",
]
