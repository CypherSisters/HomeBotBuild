"""
PuffPAI Vault View — ephemeral in-memory single-slot secure document serve.

Holds AT MOST ONE decrypted document's bytes in process memory for a short TTL
so the on-box kiosk can render a protected vault document, then zeroes and drops
them. The bytes are NEVER persisted to disk and this module NEVER logs anything
(not the bytes, not the title, not the token).

Contract:
  * mint(): a new view invalidates+zeroes any prior slot. If an active,
    un-expired view already occupies the single slot, mint REFUSES — it returns
    the BUSY sentinel rather than silently stomping the shared kiosk (concurrent
    critical section). Minting is otherwise fresh: token = token_urlsafe(32).
  * fetch(): lazy-expire, validate token + unexpired + under max_fetches,
    increment the counter, and return a COPY of the bytes. Returns None on ANY
    failure (fail-closed). The route — not this module — does the loopback check.
  * clear()/clear_all(): zero the bytearray in place, then drop the slot.
  * Every mint/fetch/has_active/active_title call lazily sweeps an expired slot,
    so stale plaintext bytes are ACTIVELY dropped rather than lingering.

Single active slot + short TTL + no re-fetch without a fresh PIN are the real
guarantees; max_fetches is only a small runaway cap.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time

#: Sentinel returned by mint() when an active, un-expired view already holds the
#: single slot. The caller must surface a "being viewed right now" message and
#: NOT stomp the shared kiosk. Compared by identity (``is BUSY``).
BUSY = object()

#: Small runaway cap on re-fetches of the same view (an iframe may re-render the
#: same src once or twice). NOT the security boundary — that is single-slot + TTL.
_DEFAULT_MAX_FETCHES = 5

_lock = threading.Lock()
# The single active slot, or None. Shape:
#   {token, data(bytearray), mime, title, doc_id, created, ttl, max_fetches, fetches}
_slot: dict | None = None


def _zero_and_drop_locked() -> None:
    """Zero the current slot's plaintext bytes in place, then drop it.

    Caller MUST hold ``_lock``. Zeroing the bytearray overwrites the decrypted
    plaintext so a lingering reference cannot recover it.
    """
    global _slot
    if _slot is not None:
        buf = _slot.get("data")
        if isinstance(buf, bytearray):
            for i in range(len(buf)):
                buf[i] = 0
        _slot = None


def _expired(slot: dict, now: float) -> bool:
    return (now - slot["created"]) > slot["ttl"]


def _sweep_locked(now: float) -> None:
    """Drop the slot if it has expired. Caller MUST hold ``_lock``."""
    if _slot is not None and _expired(_slot, now):
        _zero_and_drop_locked()


def mint(data, mime, title, doc_id, ttl, *,
         max_fetches: int = _DEFAULT_MAX_FETCHES):
    """Mint a new ephemeral view and return its token.

    Returns the BUSY sentinel (identity-compared) if an active, un-expired view
    already occupies the single slot — the caller must not stomp it. Otherwise
    zeroes any prior (expired) slot, stores a fresh copy of ``data`` as a
    bytearray, and returns a ``secrets.token_urlsafe(32)`` token.
    """
    global _slot
    now = time.monotonic()
    with _lock:
        _sweep_locked(now)
        if _slot is not None:
            # A live view already holds the single slot — refuse.
            return BUSY
        # No live slot survives; belt-and-suspenders zero of anything lingering.
        _zero_and_drop_locked()
        token = secrets.token_urlsafe(32)
        _slot = {
            "token": token,
            "data": bytearray(bytes(data)),
            "mime": mime,
            "title": title,
            "doc_id": doc_id,
            "created": now,
            "ttl": float(ttl),
            "max_fetches": max(1, int(max_fetches)),
            "fetches": 0,
        }
        return token


def fetch(token):
    """Return ``(bytes_copy, mime)`` for a valid live token, else None.

    Fail-closed: any of missing/mismatched token, expired slot, or exceeded
    max_fetches returns None. On success the fetch counter is incremented and a
    COPY of the bytes is returned (so the caller never holds a reference to the
    live bytearray this module later zeroes). The loopback check is the route's
    responsibility, not this module's.
    """
    if not token or not isinstance(token, str):
        return None
    now = time.monotonic()
    with _lock:
        _sweep_locked(now)
        slot = _slot
        if slot is None:
            return None
        # Constant-time token comparison (both ASCII urlsafe base64).
        try:
            if not hmac.compare_digest(token, slot["token"]):
                return None
        except Exception:
            return None
        if _expired(slot, now):
            _zero_and_drop_locked()
            return None
        if slot["fetches"] >= slot["max_fetches"]:
            return None
        slot["fetches"] += 1
        return bytes(slot["data"]), slot["mime"]


def clear(token) -> bool:
    """Zero+drop the slot iff ``token`` matches the active view. Returns True if
    a slot was cleared. Fail-closed on a mismatch (leaves the slot alone)."""
    with _lock:
        if _slot is None or not token or not isinstance(token, str):
            return False
        try:
            if not hmac.compare_digest(token, _slot["token"]):
                return False
        except Exception:
            return False
        _zero_and_drop_locked()
        return True


def clear_all() -> None:
    """Zero+drop any active slot unconditionally (panic/lock-all)."""
    with _lock:
        _zero_and_drop_locked()


def has_active() -> bool:
    """True if a live (un-expired) view occupies the slot right now."""
    now = time.monotonic()
    with _lock:
        _sweep_locked(now)
        return _slot is not None


def active_title():
    """The title of the live view, or None. Titles are the reconstructed
    'Type — Owner' string; this is the only field the overlay needs to read."""
    now = time.monotonic()
    with _lock:
        _sweep_locked(now)
        return _slot["title"] if _slot is not None else None


__all__ = ["BUSY", "mint", "fetch", "clear", "clear_all",
           "has_active", "active_title"]
