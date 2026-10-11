"""One choke point that opens and closes every turn-scoped thread-local.

WHY THIS EXISTS
---------------
``turn_writes`` and ``turn_health`` both keep a per-thread record of what the
CURRENT served turn did, and the honesty notice
(``api_server._pii_types_not_stored_this_turn``) consults both. Each module has
a ``begin_turn`` / ``end_turn`` pair. ``begin_turn`` is called from
``_prepare_user_message`` (the four typed chat surfaces) and from the voice
route, but nothing in a handler is guaranteed to call ``end_turn``.

Flask serves on a pool of reused worker threads, so without a choke point a
thread that finished a medication turn keeps ``active=True``, the compiled
health pattern, and ``kept=True`` until the NEXT request on that thread happens
to call ``begin_turn`` again. Two consequences, in opposite directions:

* **Over-redaction (privacy-safe, but wrong).** Any later request on that
  thread that persists an assistant reply runs
  ``turn_health.redact_turn_terms`` against the PREVIOUS turn's terms. It only
  ever removes more, so nothing leaks — but the stored transcript of an
  unrelated conversation grows ``[REDACTED-HEALTH]`` markers that describe no
  disclosure that turn made.

* **A suppressed honesty notice (NOT safe).** ``health_was_stored()`` stays
  ``True``. Any persistence path that runs WITHOUT a preceding ``begin_turn``
  therefore inherits a "yes, this turn kept a health term" answer it never
  earned, and ``_pii_types_not_stored_this_turn`` would drop
  ``health.condition`` from the notice — PuffPAI staying silent about something
  it really did discard. ``/chat/save`` is the reachable path with no
  ``begin_turn`` of its own.

WHAT THIS MODULE DOES
---------------------
Registers ONE ``before_request`` / ``teardown_request`` pair on the Flask app.

* ``before_request`` runs FIRST — ahead of the rate limiter and the API-key
  guard — so no handler, and no short-circuited 401/429 path, can ever observe
  a previous turn's record. It cannot itself short-circuit anything: it returns
  ``None`` unconditionally.
* ``teardown_request`` runs on every request including the ones that raised, so
  a handler that dies mid-turn does not leave an armed record behind for the
  next request that lands on the same thread.

``begin_turn`` stays exactly where it is. Resetting at both ends is deliberate
belt-and-braces: the entry reset is what makes the guarantee hold no matter how
a request ended, and the exit reset is what keeps a thread clean while it sits
idle in the pool.

FAIL DIRECTION
--------------
:func:`reset_all` never raises and never reports. If a record module cannot be
imported, its state cannot be cleared — but nothing in the request could have
armed or read it either, since that is the same import. Each record is cleared
independently, so a failure in one cannot leave the other armed.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

#: The turn-scoped thread-locals, by module name under ``puffpai.security``.
#: Every entry must expose a no-argument ``end_turn()`` that clears the calling
#: thread's record. Adding a third per-turn record means adding it HERE, not
#: adding another hook.
_TURN_RECORDS = ("turn_writes", "turn_health")


def reset_all() -> None:
    """Clear every turn-scoped thread-local on the calling thread.

    Never raises. Each record is cleared in its own ``try`` so one failing
    module cannot leave a sibling armed.
    """
    for name in _TURN_RECORDS:
        try:
            module = __import__(
                "puffpai.security." + name, fromlist=["end_turn"])
            module.end_turn()
        except Exception:
            continue


def _before_request():
    """Start every request with an empty turn record.

    Returns ``None`` always: registered ahead of the rate limiter and the
    API-key guard, this hook must never be able to short-circuit a request.
    """
    reset_all()
    return None


def _teardown_request(exc=None):
    """Close the turn record, including when the handler raised.

    ``exc`` is Flask's unhandled-exception argument; it is ignored on purpose —
    a failed turn must be cleaned up exactly like a successful one.
    """
    reset_all()


def install(app) -> None:
    """Wire the pair onto ``app``.

    Call this BEFORE any other ``before_request`` is registered: Flask runs
    them in registration order, and a 401 or 429 returned by an earlier hook
    stops the chain.
    """
    app.before_request(_before_request)
    app.teardown_request(_teardown_request)


__all__ = ["reset_all", "install"]
