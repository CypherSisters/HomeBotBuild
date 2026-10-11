"""Proactive speech: PuffPAI raising its hand.

One primitive — :func:`announce` — with a transport chain and a paper trail:

1. **Voice satellite, via the bus.** An ``announce`` envelope to the Steward,
   whose hardened branch (see :meth:`StewardAgent._announce`) calls
   ``assist_satellite.announce``. It rides the same bus as every other
   state-changing action, so Sentinel reviews it. When the satellite is
   offline (its entities read ``unavailable``) the attempt simply fails and
   the chain moves on — plugging the satellite back in makes announcements
   audible again with **no code change here**.
2. **The queue, always.** Every announcement — spoken or not — is appended to
   ``announcements.jsonl`` under the resolved state dir and logged to the
   agent activity feed, so any client (the kiosk, the PWA) can render or
   speak it, and nothing is ever silently dropped.

Text only, deliberately: the Steward's ``media_player``/``url`` announce
branch is NOT reachable from here. An announcement is a sentence, never a
fetch — exposing the URL half would hand every future caller of this helper
an outbound-request primitive it does not need.

Never raises: proactive speech is a courtesy, and no caller should have to
guard against its failure.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading

from puffpai.statedir import resolve_runtime_dir

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "data")

QUEUE_FILENAME = "announcements.jsonl"

#: Cursor written by the LOCAL SPEAKER consumer (the voice service). The API
#: process cannot reach PulseAudio — ``puffpai-api.service`` is a system unit
#: with ``ProtectHome=true`` and no session bus, while the voice loop is a user
#: unit that owns the mic, the Piper engine and the duck policy. So the API
#: writes the sentence here and the voice loop speaks it. One file, two
#: processes, no new socket.
CURSOR_FILENAME = "announcements.cursor"

#: Playback receipts from the LOCAL SPEAKER consumer: one record timestamp per
#: line, appended by the voice service only after the room speaker actually
#: said the sentence. The cursor cannot carry this on its own: it also moves
#: past records that were skipped (stale, not ``speak``) or whose playback
#: failed. :func:`recent` overlays these receipts so other screens see
#: ``spoken: true`` and stop re-reading what the room already heard. The flag
#: says only "the room speaker said it"; a phone may still read it aloud.
SPOKEN_FILENAME = "announcements.spoken"
LOCAL_SPEAKER_SINK = "local_speaker"

#: How stale an unspoken announcement may be and still be worth saying out
#: loud. A restart must not read out an evening's backlog.
MAX_SPEAK_AGE_SECONDS = 600.0

#: An announcement is a sentence or two. Longer text is a briefing, and
#: briefings have their own pipeline.
MAX_TEXT_CHARS = 300

#: The queue is a rolling window, not an archive — the activity feed is the
#: durable record. Kept small so every read is cheap.
MAX_QUEUE_ENTRIES = 200

_LOCK = threading.Lock()
_data_dir_cache: str | None = None


def _data_dir() -> str:
    """State dir, resolved lazily (same reasoning as zigbee_pairing's)."""
    global _data_dir_cache
    if _data_dir_cache is None:
        _data_dir_cache = resolve_runtime_dir(_DEFAULT_DATA_DIR)
    return _data_dir_cache


def queue_path() -> str:
    return os.path.join(_data_dir(), QUEUE_FILENAME)


def cursor_path() -> str:
    return os.path.join(_data_dir(), CURSOR_FILENAME)


def spoken_path() -> str:
    return os.path.join(_data_dir(), SPOKEN_FILENAME)


def reset_cache() -> None:
    """Forget the resolved state dir. For tests that move ``$PUFFPAI_DATA_DIR``."""
    global _data_dir_cache
    _data_dir_cache = None


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _default_bus():
    """The running api_server's agent team, if this process has one.

    ``sys.modules`` lookup, never an import — the established pattern (see
    assistant_tools) that keeps this module importable in tests and tools without
    dragging the whole server up.
    """
    api = sys.modules.get("puffpai.api_server")
    if api is None:
        return None
    getter = getattr(api, "_get_agent_team", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


def _try_satellite(text: str, bus) -> bool:
    """One attempt at the voice satellite. True only on a confirmed ok."""
    if bus is None:
        return False
    try:
        from puffpai.agents import Envelope
        from puffpai.agents.envelope import Budget
        env = Envelope(
            intent="announce",
            payload={"text": text},
            requester="system",
            # Honest provenance: NOT a human origin, so nothing downstream can
            # mistake a proactive courtesy for a person's command.
            origin_agent="announcer",
            budget=Budget(max_actions=2, max_seconds=10.0),
            state_changing=True,
        )
        reply = bus.send(env, to="steward")
        return bool(isinstance(reply, dict) and reply.get("ok"))
    except Exception as exc:
        logger.debug("announcer: satellite attempt failed: %s", exc)
        return False


def _append_queue(record: dict) -> bool:
    try:
        path = queue_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with _LOCK:
            lines: list[str] = []
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as fh:
                    lines = [l for l in fh.read().splitlines() if l.strip()]
            lines.append(json.dumps(record, ensure_ascii=False))
            lines = lines[-MAX_QUEUE_ENTRIES:]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        return True
    except Exception as exc:
        logger.warning("announcer: could not write the queue: %s", exc)
        return False


def announce(text: str, *, source: str = "system", bus=None,
             speak: bool = True) -> dict:
    """Say *text* to the house if a speaker is reachable; record it always.

    *speak* marks the record for the local speaker consumer
    (:func:`pending_speech`). Leave it True for anything a person in the room
    should hear; set it False for a record that is only for the feed.

    Returns ``{"ok", "spoken", "sink", "queued", "text"}``. Never raises.
    """
    clean = " ".join(str(text or "").split())[:MAX_TEXT_CHARS]
    if not clean:
        return {"ok": False, "spoken": False, "sink": None,
                "queued": False, "text": ""}

    spoken = _try_satellite(clean, bus if bus is not None else _default_bus())
    record = {
        "ts": _utcnow(),
        "text": clean,
        "source": str(source)[:40],
        "spoken": spoken,
        "sink": "assist_satellite" if spoken else None,
        "speak": bool(speak) and not spoken,
    }
    queued = _append_queue(record)

    try:
        from puffpai.agent_activity import log_agent_activity
        log_agent_activity(
            "concierge", "announcement",
            clean, "spoken" if spoken else "queued",
            {"source": record["source"], "spoken": spoken})
    except Exception as exc:
        logger.debug("announcer: activity log failed: %s", exc)

    return {"ok": True, "spoken": spoken, "sink": record["sink"],
            "queued": queued, "text": clean}


def read_cursor() -> str:
    """Timestamp of the last record the local speaker voiced ("" if none)."""
    try:
        path = cursor_path()
        if not os.path.isfile(path):
            return ""
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception as exc:
        logger.debug("announcer: cursor unreadable: %s", exc)
        return ""


def write_cursor(ts: str) -> None:
    try:
        path = cursor_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(ts or "").strip())
    except Exception as exc:
        logger.warning("announcer: could not persist the cursor: %s", exc)


def _read_spoken() -> list[str]:
    try:
        path = spoken_path()
        if not os.path.isfile(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return [l.strip() for l in fh.read().splitlines() if l.strip()]
    except Exception as exc:
        logger.debug("announcer: spoken receipts unreadable: %s", exc)
        return []


def mark_spoken(ts: str) -> None:
    """Record that the local room speaker said the record stamped *ts*.

    Called by the voice service after playback returned without error. Kept
    as a rolling window the size of the queue. Never raises.
    """
    ts = str(ts or "").strip()
    if not ts:
        return
    try:
        path = spoken_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with _LOCK:
            stamps = [t for t in _read_spoken() if t != ts]
            stamps.append(ts)
            stamps = stamps[-MAX_QUEUE_ENTRIES:]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(stamps) + "\n")
    except Exception as exc:
        logger.warning("announcer: could not record playback: %s", exc)


def pending_speech(max_age_seconds: float = MAX_SPEAK_AGE_SECONDS,
                   now=None) -> list[dict]:
    """Records the local speaker still owes the room, oldest first.

    Everything newer than the persisted cursor, marked ``speak``, and not
    older than *max_age_seconds* — so a voice service that has been down all
    evening comes back and says the one thing that is still relevant instead
    of reciting the log. Never raises.
    """
    from datetime import datetime, timezone
    cursor = read_cursor()
    stamp = now if now is not None else datetime.now(timezone.utc)
    out: list[dict] = []
    for record in reversed(recent(MAX_QUEUE_ENTRIES)):   # oldest first
        ts = str(record.get("ts") or "")
        if not ts or (cursor and ts <= cursor):
            continue
        if not record.get("speak") or record.get("spoken"):
            continue
        try:
            age = (stamp - datetime.fromisoformat(ts)).total_seconds()
        except Exception:
            age = 0.0
        if age > max_age_seconds:
            continue
        out.append(record)
    return out


def recent(limit: int = 20) -> list[dict]:
    """Newest-first tail of the queue, for clients that render or speak it."""
    limit = max(1, min(int(limit), MAX_QUEUE_ENTRIES))
    try:
        path = queue_path()
        if not os.path.isfile(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            lines = [l for l in fh.read().splitlines() if l.strip()]
        spoken = set(_read_spoken())
        out = []
        for line in lines[-limit:]:
            try:
                record = json.loads(line)
            except Exception:
                continue
            if (isinstance(record, dict) and not record.get("spoken")
                    and str(record.get("ts") or "") in spoken):
                record["spoken"] = True
                record["sink"] = LOCAL_SPEAKER_SINK
            out.append(record)
        out.reverse()
        return out
    except Exception as exc:
        logger.warning("announcer: could not read the queue: %s", exc)
        return []
