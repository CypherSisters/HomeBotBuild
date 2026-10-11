"""
Proactive suggestions — what PuffPAI noticed about a document you just sent.

After a document finishes indexing, a background pass classifies it and
queues ONE suggestion the dashboard can surface ("I found 9 events in Fall
class schedule — want me to add them to your calendar?").

Nothing here executes anything. A schedule suggestion carries pre-extracted
calendar CANDIDATES; accepting it hands those back to the dashboard's normal
preview-confirm flow, where the user still approves every event. Same rule as
calendar_ingest: an LLM's reading of a PDF never lands on the family calendar
unreviewed.

Store persists at app_data/suggestions.json.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

from puffpai import statedir

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Shipped default for the directory holding the store — ``<repo>/app_data``,
#: the same expression :mod:`puffpai.app_data` uses, with the same
#: ``env_subdir``. The two must resolve identically under BOTH env states or
#: suggestions.json separates from the app data it is about.
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "app_data")

#: Resolved app-data directory; ``None`` = not resolved yet. Reset by
#: ``importlib.reload``.
_data_dir_cache: str | None = None

#: Test/ops override hook: a path here outranks both ``$PUFFPAI_DATA_DIR`` and
#: the shipped default; ``None`` means resolve. ``tests/test_suggestions.py``
#: isolates the store through this name.
#:
#: Deliberately a plain global rather than a module ``__getattr__`` shim.
#: ``monkeypatch.setattr`` against a name that only ``__getattr__`` synthesizes
#: records the SYNTHESIZED value as the "old" value, so teardown does not remove
#: the name — it PINS that value into the module globals. From then on the pin
#: outranks ``$PUFFPAI_DATA_DIR`` for the rest of the process and sends writes
#: back into the real repo tree. A plain ``None`` restores cleanly, and
#: ``mock.patch`` works with either shape.
_STORE_PATH = None


def _data_dir() -> str:
    """App-data directory, resolved on FIRST USE and cached.

    Lazy on purpose — see :func:`puffpai.statedir.resolve_runtime_dir`:
    resolving at import time would pull :mod:`puffpai.config` (and the secrets
    provider behind it) into this module's import graph.
    """
    global _data_dir_cache
    if _data_dir_cache is None:
        _data_dir_cache = statedir.resolve_runtime_dir(
            _DEFAULT_DATA_DIR, env_subdir="app_data")
    return _data_dir_cache


def _store_path() -> str:
    """Path of the suggestion store, honouring an explicit module override."""
    if _STORE_PATH:
        return os.fspath(_STORE_PATH)
    return os.path.join(_data_dir(), "suggestions.json")


_lock = threading.RLock()

# A suggestion is a nudge about something that just happened; after two days
# it is noise, not news.
TTL_HOURS = 48

# Only the first slice is classified — enough to tell a class schedule from
# an appliance manual without paying for a long-document pass.
CLASSIFY_CHARS = 4000

VALID_KINDS = ('schedule', 'manual', 'recipe', 'receipt', 'contact_info', 'general')

_CLASSIFY_PROMPT = """Classify the document below into exactly one kind:

  schedule     — dates/times of events: school schedules, class timetables,
                 game calendars, appointment lists, itineraries
  manual       — instructions or reference for a thing: appliance manuals,
                 how-to guides, warranties, policy documents
  recipe       — cooking instructions with ingredients
  receipt      — a record of a purchase: receipts, invoices, order confirmations
  contact_info — names with phone numbers, emails or addresses: rosters,
                 contact sheets, business cards
  general      — anything else

Reply with ONLY a JSON object, no prose:
  {{"kind": one of {kinds}, "confidence": 0.0-1.0,
    "summary": "one short sentence describing the document"}}

FILENAME: {filename}
DOCUMENT:
{document}
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ── Store persistence ────────────────────────────────────────────────────────

def _load() -> list[dict]:
    try:
        with open(_store_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save(items: list[dict]) -> None:
    """Persist the suggestion store.

    Second writer into ``app_data/`` after :mod:`puffpai.app_data`, and it
    shares the same helper so the two cannot drift on mode or atomicity. The
    previous version was already temp+rename, but created the temp at the
    process umask, which is exactly how ``suggestions.json`` ended up 0644 —
    and a suggestion is derived from what the household actually did, so it is
    household-internal even though it holds no raw app document.
    """
    from puffpai.security.app_data_store import write_json_secure
    write_json_secure(_store_path(), items, indent=2, ensure_ascii=False,
                      default=None)


def _parse_iso(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _expire(items: list[dict]) -> bool:
    """Flip past-TTL pending records to 'expired'. Returns True if changed.

    Lazy: every read expires, so no cron job has to run on the Orin.
    """
    now = _utcnow()
    changed = False
    for s in items:
        if s.get("status") != "pending":
            continue
        exp = _parse_iso(s.get("expires_at"))
        if exp is not None and exp <= now:
            s["status"] = "expired"
            changed = True
    return changed


# ── Read / act ───────────────────────────────────────────────────────────────

def list_pending() -> list[dict]:
    """Pending suggestions, newest first, with expiry applied."""
    with _lock:
        items = _load()
        if _expire(items):
            _save(items)
        pending = [s for s in items if s.get("status") == "pending"]
    pending.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return pending


def get(suggestion_id: str) -> dict | None:
    with _lock:
        items = _load()
        if _expire(items):
            _save(items)
        return next((s for s in items if s.get("id") == suggestion_id), None)


def _set_status(suggestion_id: str, status: str) -> tuple[dict | None, str | None]:
    """Move a pending suggestion to `status`.

    Returns (record, error) where error is 'not_found' or 'not_pending'.
    """
    with _lock:
        items = _load()
        _expire(items)
        target = next((s for s in items if s.get("id") == suggestion_id), None)
        if target is None:
            return None, "not_found"
        if target.get("status") != "pending":
            return target, "not_pending"
        target["status"] = status
        target["resolved_at"] = _iso(_utcnow())
        _save(items)
        return target, None


def accept(suggestion_id: str) -> tuple[dict | None, str | None]:
    return _set_status(suggestion_id, "accepted")


def dismiss(suggestion_id: str) -> tuple[dict | None, str | None]:
    return _set_status(suggestion_id, "dismissed")


# ── The spoken offer ───────────────────────────────────────
#
# A suggestion is a CARD on the dashboard. But the person who photographed a
# recipe with their phone is in the kitchen, not at the dashboard, so an
# actionable suggestion is also said out loud — ONCE — and answered by the next
# "yes"/"no" (api_server._try_upload_offer_reply). Saying it twice, or saying
# it about a document nobody can act on, is noise; both are prevented here.

#: Action types worth saying out loud. Everything else is a card only.
OFFERABLE_ACTIONS = ("recipe_import", "calendar_import")

#: How long an unanswered spoken offer stays answerable by "yes". Longer than
#: pending_clarifier's two minutes because the offer arrives unprompted — the
#: person may be mid-task — and shorter than the suggestion's own 48 h, because
#: a "yes" ten minutes later is an answer and an hour later is a coincidence.
OFFER_TTL_SECONDS = 600.0


def _mutate(suggestion_id: str, change) -> dict | None:
    """Apply *change* to one stored record under the lock. Returns it."""
    with _lock:
        items = _load()
        target = next((s for s in items if s.get("id") == suggestion_id), None)
        if target is None:
            return None
        change(target)
        _save(items)
        return target


def mark_offered(suggestion_id: str) -> dict | None:
    """Stamp a record as SPOKEN, so it is never offered out loud twice."""
    return _mutate(suggestion_id, lambda s: s.update(
        {"offered_at": _iso(_utcnow())}))


def clear_offer(suggestion_id: str) -> dict | None:
    """Forget that the offer is outstanding; the card stays pending.

    Called when the turn after the offer was about something else. "It
    forgot" is the right failure here — a "yes" to an unrelated question must
    never save a recipe.
    """
    return _mutate(suggestion_id, lambda s: s.update({"offered_at": None}))


def offered_pending() -> dict | None:
    """The spoken offer still waiting on an answer, or None."""
    now = _utcnow()
    best = None
    for record in list_pending():
        offered = _parse_iso(record.get("offered_at"))
        if offered is None:
            continue
        if (now - offered).total_seconds() > OFFER_TTL_SECONDS:
            continue
        if best is None or str(record.get("offered_at")) > str(best.get("offered_at")):
            best = record
    return best


def display_title(record: dict) -> str:
    """The record's title as a person would say it (no "recipe: " prefix)."""
    title = str((record or {}).get("title") or "").strip()
    kind = str((record or {}).get("kind") or "").strip().lower()
    prefix = kind + ":"
    if kind and title.lower().startswith(prefix):
        title = title[len(prefix):].strip()
    return title or str((record or {}).get("doc_name") or "")


def offer_text(record: dict) -> str | None:
    """The one sentence this suggestion is worth saying, or None."""
    action = (record or {}).get("action") or {}
    kind = action.get("type")
    if kind not in OFFERABLE_ACTIONS:
        return None
    title = display_title(record)
    if kind == "recipe_import":
        return (f"That looks like a recipe — {title}. "
                f"Want me to save it to your recipes?")
    count = len((action.get("params") or {}).get("candidates") or [])
    plural = "event" if count == 1 else "events"
    it = "it" if count == 1 else "them"
    return (f"That looks like a schedule — {title}, with {count} {plural}. "
            f"Want me to put {it} on your calendar?")


def speak_offer(record: dict, announce_fn=None) -> bool:
    """Announce this suggestion once. Returns whether anything was said.

    Never raises: a proactive courtesy must not turn a successful upload into
    an error, and the dashboard card stands whether or not the box spoke.
    """
    if not record or record.get("offered_at"):
        return False
    text = offer_text(record)
    if not text:
        return False
    if announce_fn is None:
        from puffpai import announcer

        def announce_fn(sentence):
            return announcer.announce(sentence, source="suggestions")
    try:
        announce_fn(text)
    except Exception as e:                  # pragma: no cover - courtesy only
        logger.warning("Could not speak suggestion offer: %s", e)
        return False
    mark_offered(record.get("id"))
    record["offered_at"] = _iso(_utcnow())
    return True


def _safe_record(record: dict) -> dict:
    """Guarantee the record survives json.dump with allow_nan=False.

    A NaN/Inf anywhere (a poisoned confidence, say) makes json.dump emit a bare
    NaN/Infinity token that browsers' JSON.parse rejects, killing the entire
    pending queue for every client. If the record can't round-trip cleanly,
    sanitize confidence and retry; if it still can't, fall back to a minimal
    safe record so the suggestion still appears rather than vanishing.
    """
    try:
        json.dumps(record, allow_nan=False)
        return record
    except (ValueError, TypeError):
        pass
    record = dict(record)
    conf = record.get("confidence")
    if isinstance(conf, (int, float)) and math.isfinite(conf):
        record["confidence"] = min(max(float(conf), 0.0), 1.0)
    else:
        record["confidence"] = 0.0
    try:
        json.dumps(record, allow_nan=False)
        return record
    except (ValueError, TypeError):
        now = _utcnow()
        return {
            "id": str(record.get("id") or uuid.uuid4()),
            "created_at": str(record.get("created_at") or _iso(now)),
            "expires_at": str(record.get("expires_at")
                              or _iso(now + timedelta(hours=TTL_HOURS))),
            "doc_name": str(record.get("doc_name") or "")[:200],
            "collection": "",
            "upload_id": "",
            "title": str(record.get("doc_name") or "")[:200],
            "offered_at": None,
            "zone": "knowledge",
            "kind": "general",
            "confidence": 0.0,
            "summary": "",
            "message": "I added a document to your library.",
            "action": {"type": "none", "params": {}},
            "status": "pending",
        }


def add(record: dict) -> dict:
    record = _safe_record(record)
    with _lock:
        items = _load()
        _expire(items)
        items.append(record)
        _save(items)
    return record


# ── Classification ───────────────────────────────────────────────────────────

def _json_object(reply: str) -> dict | None:
    """Pull one JSON object out of an LLM reply (fences, prose and all)."""
    if not reply:
        return None
    text = reply.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def classify_document(text: str, filename: str, llm_chat=None) -> dict:
    """One LLM pass over the head of a document.

    Returns {kind, confidence, summary}. Never raises: an unreachable or
    babbling model degrades to a low-confidence 'general', which produces a
    harmless "it's in the library" suggestion.
    """
    doc = (text or "")[:CLASSIFY_CHARS]
    fallback = {"kind": "general", "confidence": 0.0, "summary": ""}
    if not doc.strip() or llm_chat is None:
        return fallback

    prompt = _CLASSIFY_PROMPT.format(
        kinds=list(VALID_KINDS), filename=filename or "unknown", document=doc)
    try:
        reply = llm_chat([{"role": "user", "content": prompt}]) or ""
    except Exception as e:
        logger.warning("Suggestion classify failed for %s: %s", filename, e)
        return fallback

    parsed = _json_object(reply)
    if parsed is None:
        return fallback

    kind = str(parsed.get("kind") or "").lower().strip()
    if kind not in VALID_KINDS:
        kind = "general"
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    # float("NaN"/"Infinity") parses fine but a non-finite value serializes as
    # a bare NaN/Infinity token, which browsers' JSON.parse rejects — poisoning
    # the whole pending queue. Coerce anything non-finite to a safe default.
    if not math.isfinite(confidence):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)
    summary = str(parsed.get("summary") or "").strip()[:300]
    return {"kind": kind, "confidence": confidence, "summary": summary}


# ── Titles ─────────────────────────────────────────────────
#
# A phone upload arrives named ``IMG_4821.jpg``, which is not what the
# household calls it. Either they name it on send (the ``title`` form field) or
# Puff labels it from what it read: "recipe: Chocolate chip cookies". A label
# is one short line, never a summary — it is read out loud and printed in a
# list.

#: Longest auto-derived label. Past this it stops being a name.
TITLE_MAX = 40

#: A line with no letters in it (page numbers, rules, bullets) names nothing.
_UNTITLED_LINE_RE = re.compile(r"^[^A-Za-z]*$")


def _first_meaningful_line(text: str) -> str:
    """The first line of a document that could be its heading."""
    for raw in str(text or "").splitlines():
        line = " ".join(raw.split())
        if len(line) < 3 or _UNTITLED_LINE_RE.match(line):
            continue
        if len(line) > TITLE_MAX:
            cut = line[:TITLE_MAX].rsplit(" ", 1)[0]
            line = (cut or line[:TITLE_MAX])
        return line.strip(" ,;:-—")
    return ""


def auto_title(kind: str, text: str, filename: str = "") -> str:
    """Puff's own label: ``"<kind>: <heading>"``, else the filename.

    A ``general`` document has nothing to say about itself beyond its own
    first line, so it keeps the filename the household will recognise.
    """
    kind = str(kind or "").strip().lower()
    line = _first_meaningful_line(text)
    if line and kind and kind != "general":
        return f"{kind}: {line}"
    return str(filename or "") or line


# ── Suggestion building ──────────────────────────────────────────────────────

def _message_for(kind: str, doc_name: str) -> str:
    if kind == "manual":
        return f"I filed {doc_name} in the library — ask me anything from it."
    if kind == "recipe":
        return f"Got {doc_name} — the recipe is searchable now, so just ask me for it."
    if kind == "receipt":
        return f"Filed {doc_name}. Ask me about what's on it any time."
    if kind == "contact_info":
        return f"Saved {doc_name} — ask me for anyone's details from it."
    return f"I read {doc_name} and added it to your library."


def build_suggestion(doc_name: str, text: str, collection: str = "",
                     zone: str = "knowledge", llm_chat=None,
                     title: str = "", upload_id: str = "") -> dict:
    """Classify a freshly-indexed document and shape one suggestion record.

    Not stored — see queue_suggestion_async / add.
    """
    result = classify_document(text, doc_name, llm_chat=llm_chat)
    kind = result["kind"]
    action = {"type": "none", "params": {}}
    message = None

    if kind == "schedule":
        from puffpai.calendar_ingest import ingest_text
        try:
            ingested = ingest_text(doc_name, text or "", llm_chat=llm_chat)
        except Exception as e:
            logger.warning("Suggestion calendar extraction failed for %s: %s",
                           doc_name, e)
            ingested = {"events": [], "notes": [], "method": "none"}
        events = ingested.get("events") or []
        if events:
            action = {"type": "calendar_import",
                      "params": {"candidates": events,
                                 "notes": ingested.get("notes") or [],
                                 "method": ingested.get("method")}}
            plural = "event" if len(events) == 1 else "events"
            message = (f"You sent me {doc_name} — I found {len(events)} {plural}. "
                       f"Want me to add them to your calendar?")
        else:
            # A schedule we can't read any dates out of is just a document.
            kind = "general"

    elif kind == "recipe":
        from puffpai.recipe_ingest import ingest_text
        try:
            ingested = ingest_text(doc_name, text or "", llm_chat=llm_chat)
        except Exception as e:
            logger.warning("Suggestion recipe extraction failed for %s: %s",
                           doc_name, e)
            ingested = {"recipes": [], "notes": [], "method": "none"}
        recipes = ingested.get("recipes") or []
        if recipes:
            action = {"type": "recipe_import",
                      "params": {"recipes": recipes,
                                 "notes": ingested.get("notes") or [],
                                 "method": ingested.get("method")}}
            n = len(recipes)
            message = (f"You sent me {doc_name} — I found {n} "
                       f"recipe{'s' if n != 1 else ''}. Want me to save "
                       f"{'them' if n != 1 else 'it'} to FreshTable?")
        else:
            # A recipe we can't read ingredients out of is still searchable in
            # the library — fall through to the generic recipe message.
            pass

    created = _utcnow()
    return {
        "id": str(uuid.uuid4()),
        "created_at": _iso(created),
        "expires_at": _iso(created + timedelta(hours=TTL_HOURS)),
        "doc_name": doc_name,
        "collection": collection,
        "upload_id": upload_id or collection or "",
        # Given on send, or written here from what the classifier read.
        "title": str(title or "").strip() or auto_title(kind, text, doc_name),
        "zone": zone,
        "kind": kind,
        # Set by speak_offer the one time this is said out loud.
        "offered_at": None,
        "confidence": result["confidence"],
        "summary": result["summary"],
        "message": message or _message_for(kind, doc_name),
        "action": action,
        "status": "pending",
    }


def queue_suggestion(doc_name: str, text: str, collection: str = "",
                     zone: str = "knowledge", llm_chat=None,
                     title: str = "", upload_id: str = "",
                     on_title=None, announce_fn=None,
                     speak: bool = False) -> dict | None:
    """Build and store one suggestion. Returns the record, or None on failure.

    *on_title* is called with the record's title when the household did NOT
    name the document themselves — that is the only case where Puff's own
    label should replace what the catalog already shows.

    *speak* is the UPLOAD path's business, so it defaults off: a caller that
    builds a suggestion for its own reasons must not make the house talk.
    ``queue_suggestion_async`` — the one the upload route uses — turns it on.
    """
    try:
        record = add(build_suggestion(
            doc_name, text, collection=collection, zone=zone,
            llm_chat=llm_chat, title=title, upload_id=upload_id))
    except Exception as e:
        logger.error("Could not queue suggestion for %s: %s", doc_name, e)
        return None
    if on_title is not None and not str(title or "").strip():
        try:
            on_title(record.get("title") or "")
        except Exception as e:
            logger.warning("Could not store title for %s: %s", doc_name, e)
    if speak:
        speak_offer(record, announce_fn=announce_fn)
    return record


def queue_suggestion_async(doc_name: str, text: str, collection: str = "",
                           zone: str = "knowledge", llm_chat=None,
                           title: str = "", upload_id: str = "",
                           on_title=None, announce_fn=None,
                           speak: bool = True) -> threading.Thread:
    """Fire-and-forget queue_suggestion on a daemon thread.

    The upload response must not wait on an LLM pass that can take a minute on
    a cold model, and a failure here must never turn a successful index into an
    error the user sees.
    """
    t = threading.Thread(
        target=queue_suggestion,
        kwargs={"doc_name": doc_name, "text": text, "collection": collection,
                "zone": zone, "llm_chat": llm_chat, "title": title,
                "upload_id": upload_id, "on_title": on_title,
                "announce_fn": announce_fn, "speak": speak},
        name="suggestion-classify", daemon=True)
    t.start()
    return t
