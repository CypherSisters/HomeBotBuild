"""
PuffPAI Dashboard Display — the "pull it up on screen" seam.

ONE place holds what PuffPAI has been asked to *display*: a TED talk, a
Wikipedia article, a document, a URL. Voice/LLM sets it (see the show tool and
the _try_show fastpath in api_server); the dashboard polls GET /dashboard/display
and renders it in an overlay viewer. Same single-source-of-truth principle as
the widget registry — the "current display" is declared in exactly one place,
so voice, chat, and the kiosk agree on what is on screen.

State persists at data/dashboard_display.json ($PUFFPAI_DATA_DIR when set) so a
backend restart doesn't blank the kiosk mid-view. A displayed item carries a
monotonically-increasing ``seq`` so the frontend can cheaply tell "changed"
from "same" while polling.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from puffpai.statedir import resolve_runtime_dir

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_STATE_PATH: str | None = None  # test/ops override hook
_lock = threading.Lock()

# Kinds the frontend viewer knows how to render.
KIND_KNOWLEDGE = "knowledge"   # an article/asset served by /knowledge/library/...
KIND_URL = "url"               # a same-origin URL (e.g. an app page)
KIND_VAULT = "vault"           # a Zone-1 ephemeral secure doc (in-memory only)
#: A route Puff just answered. ``meta.route_id`` names
#: the record at GET /maps/routes/<id>; ``url`` is its print page. See
#: docs/MAPS_ROUTE_API.md.
KIND_DIRECTIONS = "directions"
KINDS = (KIND_KNOWLEDGE, KIND_URL, KIND_DIRECTIONS)

# ── In-memory ephemeral overlay (Zone-1 vault docs) — NEVER persisted ────────
# A vault document is pulled up as an EPHEMERAL overlay that takes precedence
# over the persisted ``current`` display and auto-expires. It lives ONLY in
# process memory — the token URL, title, and secure flag NEVER touch
# dashboard_display.json (that file would otherwise leak "a doc labeled X was
# viewed at time T"). A separate monotonic ``_ephemeral_epoch`` is folded into
# the polled ``seq`` so the kiosk notices every ephemeral set/clear/expire even
# concurrently with a persisted set_display (their counters never collide).
_ephemeral: dict | None = None       # {kind, url, title, secure, created, ttl}
_ephemeral_epoch: int = 0

# ── Zone-2 challenge-response pending slot — query phrase ONLY, never a PIN ───
# When a "show me <sensitive doc>" ask is recognized, we stash ONLY the words
# the user said and speak a challenge for the PIN. The PIN arrives on the next
# turn and is captured by the api_server fastpath — it is NEVER stored here.
_pending_vault: dict | None = None    # {query_phrase, created}
_PENDING_VAULT_TTL = 45.0             # seconds


def _state_path() -> str:
    if _STATE_PATH:
        return _STATE_PATH
    return os.path.join(
        resolve_runtime_dir(os.path.join(_REPO_ROOT, "data")),
        "dashboard_display.json")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(state: dict) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def get_display() -> dict | None:
    """The item currently pulled up, or None if the screen is clear."""
    with _lock:
        st = _load()
        return st.get("current")


def get_state() -> dict:
    """Full state incl. the seq counter (what the dashboard polls).

    The in-memory ephemeral overlay (a Zone-1 vault doc) takes PRECEDENCE over
    the persisted ``current`` while it is active and un-expired; on expiry the
    poll falls back to ``current``. ``seq`` = persisted_seq + ephemeral_epoch so
    it is monotonic and changes on ANY change (persisted OR ephemeral) with no
    collision.
    """
    now = time.monotonic()
    with _lock:
        _sweep_ephemeral_locked(now)
        st = _load()
        persisted_seq = int(st.get("seq", 0))
        if _ephemeral is not None:
            current = {
                "kind": _ephemeral["kind"],
                "url": _ephemeral["url"],
                "title": _ephemeral["title"],
                "secure": _ephemeral["secure"],
                # The frontend's PRIMARY auto-clear is a client hard-timer; it must
                # match the backend TTL (esp. 45s for STRICT/HEALTH docs) or it
                # over-shows the doc. Emit both so ShowViewer never falls back to a
                # generic 60s that would exceed a 45s server TTL.
                "ttl": _ephemeral["ttl"],
                "strict": _ephemeral.get("strict", False),
                "source": "vault",
            }
        else:
            current = st.get("current")
        return {"current": current, "seq": persisted_seq + _ephemeral_epoch}


def set_display(kind: str, url: str, title: str = "",
                source: str = "voice", meta: dict | None = None) -> dict:
    """Pull an item up on the dashboard. Returns the stored display object.

    ``url`` is same-origin only (a /knowledge/... path or an app page) — the
    viewer renders it in an iframe, so an off-site URL is rejected here rather
    than trusted by the browser.
    """
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"unknown display kind '{kind}'")
    url = (url or "").strip()
    if not url.startswith("/"):
        raise ValueError("display url must be a same-origin path starting with '/'")
    item = {
        "kind": kind,
        "url": url,
        "title": title or "",
        "source": source,
        "meta": dict(meta or {}),
        "at": _utcnow(),
    }
    with _lock:
        st = _load()
        st["current"] = item
        st["seq"] = int(st.get("seq", 0)) + 1
        item["seq"] = st["seq"]
        _save(st)
    return item


def clear_display(source: str = "voice") -> bool:
    """Clear the screen. Returns True if something was showing."""
    with _lock:
        st = _load()
        had = st.get("current") is not None
        st["current"] = None
        st["seq"] = int(st.get("seq", 0)) + 1
        _save(st)
    return had


# ── In-memory ephemeral overlay (Zone-1 vault docs) ──────────────────────────

def _ephemeral_expired_locked(now: float) -> bool:
    return _ephemeral is not None and (now - _ephemeral["created"]) > _ephemeral["ttl"]


def _sweep_ephemeral_locked(now: float) -> None:
    """Drop an expired ephemeral overlay and bump the epoch. Holds _lock."""
    global _ephemeral, _ephemeral_epoch
    if _ephemeral_expired_locked(now):
        _ephemeral = None
        _ephemeral_epoch += 1


def set_ephemeral_display(kind: str, url: str, title: str = "",
                          ttl: float = 90, secure: bool = True,
                          strict: bool = False) -> dict:
    """Pull an ephemeral (in-memory, auto-expiring) item up, taking precedence
    over the persisted display. NEVER written to dashboard_display.json.

    ``url`` is a same-origin path (e.g. ``/vault/view/<token>``); the frontend
    is responsible for building the absolute backend origin for a secure item.
    ``ttl`` is the on-screen TTL the frontend's hard-timer should match; ``strict``
    marks the higher-sensitivity (STRICT/HEALTH) docs so the frontend can pick the
    shorter timer even if ``ttl`` is ever dropped.
    """
    global _ephemeral, _ephemeral_epoch
    url = (url or "").strip()
    if not url.startswith("/"):
        raise ValueError("ephemeral display url must be a path starting with '/'")
    item = {
        "kind": (kind or KIND_VAULT).strip().lower(),
        "url": url,
        "title": title or "",
        "secure": bool(secure),
        "strict": bool(strict),
        "created": time.monotonic(),
        "ttl": float(ttl),
    }
    with _lock:
        _ephemeral = item
        _ephemeral_epoch += 1
    # Return a disk-safe view (no monotonic 'created').
    return {"kind": item["kind"], "url": item["url"], "title": item["title"],
            "secure": item["secure"], "strict": item["strict"], "ttl": item["ttl"]}


def clear_ephemeral_display() -> bool:
    """Drop the ephemeral overlay. Returns True if one was active."""
    global _ephemeral, _ephemeral_epoch
    with _lock:
        had = _ephemeral is not None
        _ephemeral = None
        _ephemeral_epoch += 1
    return had


def ephemeral_active() -> bool:
    """True if an ephemeral overlay is live (un-expired) right now."""
    now = time.monotonic()
    with _lock:
        _sweep_ephemeral_locked(now)
        return _ephemeral is not None


# ── Zone-2 challenge-response pending slot ───────────────────────────────────

def set_pending_vault(query_phrase: str) -> None:
    """Record a pending vault-display request (the query phrase ONLY). A new
    request replaces any prior one. Never holds a PIN or vault contents."""
    global _pending_vault
    with _lock:
        _pending_vault = {"query_phrase": query_phrase,
                          "created": time.monotonic()}


def pending_vault_active() -> bool:
    """True if a live (un-expired) pending vault-display request exists."""
    global _pending_vault
    with _lock:
        if _pending_vault is None:
            return False
        if (time.monotonic() - _pending_vault["created"]) > _PENDING_VAULT_TTL:
            _pending_vault = None
            return False
        return True


def take_pending_vault():
    """Return+clear the pending query phrase if live, else None (and clear)."""
    global _pending_vault
    with _lock:
        if _pending_vault is None:
            return None
        live = (time.monotonic() - _pending_vault["created"]) <= _PENDING_VAULT_TTL
        phrase = _pending_vault["query_phrase"] if live else None
        _pending_vault = None
        return phrase


def clear_pending_vault() -> None:
    """Discard any pending vault-display request."""
    global _pending_vault
    with _lock:
        _pending_vault = None


# ── Resolve a "show me X" ask — SOURCE-AWARE ─────────────────────────────────
# "Show me X" is a DISPLAY intent (distinct from "tell me X", which speaks). The
# SOURCE depends on what X is, and the routing must respect the Zone-1/Zone-2
# trust boundary: the Zone-2 brain must
# NOT hold an index of what's in the Zone-1 vault. It recognizes a sensitive
# doc by its KIND (birth certificate, passport, …) and hands off for a
# PIN-gated Zone-1 lookup — it never confirms what is actually stored.

_TOPIC_RE = re.compile(
    r"\b(?:about|on|covering|regarding|that covers?|of|for)\b\s+(.+)$",
    re.IGNORECASE)
_STRIP_RE = re.compile(
    r"\b(show me|pull up|bring up|put up|display|open up|the|a|an|me|my|please|"
    r"video|talk|ted\s*talk|ted|article|page|document|entry|wikipedia|wiki|"
    r"lecture|clip|that|covers?)\b", re.IGNORECASE)

# Sensitive document TYPES — recognized by KIND, never by a vault index.
_VAULT_TYPE_RE = re.compile(
    r"\b(birth certificate|death certificate|marriage certificate|passport|"
    r"social security|ssn|driver'?s? licen[cs]e|medical record|health record|"
    r"will|living will|deed|title deed|mortgage|tax return|w-?2|1099|"
    r"insurance (?:card|policy)|bank statement|account number|routing number|"
    r"prescription|immunization record|vaccination record)\b", re.IGNORECASE)
# A personal possessive ("my", "grandma's", "<a name>'s") …
_MY_DOC_RE = re.compile(
    r"\b(my|our|his|her|their|grandma'?s|grandpa'?s|mom'?s|mum'?s|dad'?s|"
    r"[a-z]+'s)\b", re.IGNORECASE)
# … plus a document noun → the user's own uploads.
_DOC_NOUN_RE = re.compile(
    r"\b(recipe|letter|photo|photos|picture|pictures|note|notes|document|doc|"
    r"scan|receipt|manual|homework|essay|report|resume|spreadsheet|form|"
    r"itinerary|ticket|flyer)\b", re.IGNORECASE)


def classify_source(query: str) -> str:
    """Which source a "show me X" request points at — WITHOUT consulting the
    vault. Sensitive doc TYPES → 'vault' (by kind, not by any index of what is
    actually stored). A personal possessive + a document noun → 'documents'
    (the user's own uploads). Everything else → 'knowledge' (the ZIM shelves).
    """
    low = (query or "").lower()
    if _VAULT_TYPE_RE.search(low):
        return "vault"
    if _MY_DOC_RE.search(low) and _DOC_NOUN_RE.search(low):
        return "documents"
    return "knowledge"


def _extract_topic(query: str) -> str:
    low = (query or "").lower()
    m = _TOPIC_RE.search(low)
    topic = (m.group(1) if m else _STRIP_RE.sub(" ", query)).strip(" .?!,")
    return " ".join(topic.split())


# ── App / dashboard pages ("pull up my calendar", "show me the firewall") ────
# "Show me X" can also mean "put one of my own pages on screen". These are
# same-origin dashboard routes the overlay iframes directly (KIND_URL) — no
# search, no decode, so they render instantly and crisply. The name→route table
# mirrors the sidebar (menuAside.ts) so a spoken name matches what the user
# sees. Keywords are matched whole-word, longest-first, so "smart home" wins
# over "home" and multi-word names are not shadowed by their parts.
_APP_PAGES = (
    ("/dashboard/apps/calendar",   "Calendar",     ("my calendar", "calendar", "my schedule", "schedule")),
    ("/dashboard/apps/alarmflow",  "AlarmFlow",    ("alarmflow", "my alarms", "alarms")),
    ("/dashboard/apps/freshtable", "FreshTable",   ("freshtable", "fresh table", "grocery list", "groceries", "meal plan")),
    ("/dashboard/apps/fitfam",     "FitFam",       ("fitfam", "fit fam", "my workouts", "my fitness")),
    ("/dashboard/apps/trivia",     "Trivia",       ("trivia game", "trivia")),
    ("/dashboard/firewall",        "Firewall",     ("firewall",)),
    ("/dashboard/homeassistant",   "Smart Home",   ("smart home", "home assistant", "smarthome")),
    ("/dashboard/documents",       "My Documents", ("my documents", "my uploads", "my files", "documents page")),
    ("/dashboard/knowledge",       "Knowledge",    ("knowledge base", "knowledge library", "my library")),
    ("/dashboard/media",           "Media",        ("media hub", "media page", "media center")),
    ("/dashboard/media/jellyfin",  "Jellyfin",     ("jellyfin", "jellyfin library", "my movies", "my shows")),
    ("/dashboard/agents",          "Agent Team",   ("agent team", "the agents", "agent roster")),
    ("/dashboard/workflows",       "Workflows",    ("my workflows", "workflows", "automations")),
    ("/dashboard/settings",        "Settings",     ("settings",)),
    ("/dashboard/chat",            "Chat",         ("chat page", "the chat")),
    ("/dashboard/device",          "Device",       ("device page", "device stats", "system stats")),
    ("/dashboard",                 "Dashboard",    ("home screen", "main dashboard", "home dashboard")),
)

# Flattened (keyword, route, title), longest keyword first — built once at import.
_APP_PAGE_KEYWORDS = sorted(
    ((kw, route, title) for route, title, kws in _APP_PAGES for kw in kws),
    key=lambda t: len(t[0]), reverse=True)


def resolve_app_page(query: str) -> dict | None:
    """Map "show me / pull up <one of my pages>" to a same-origin dashboard
    route (KIND_URL), or None if the query names no known page. Checked BEFORE
    source classification so a page name never falls through to a knowledge or
    document search. Whole-word, longest-keyword-first so "smart home" is not
    shadowed by "home"."""
    low = (query or "").lower()
    for kw, route, title in _APP_PAGE_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", low):
            return {"kind": KIND_URL, "url": route, "title": title, "app": True}
    return None


def resolve_book(query: str, client_id=None) -> dict | None:
    """"Show me Moby Dick" / "the Walking Dead comic" → the Calibre reader.

    Thin delegate: all of it lives in :mod:`puffpai.books.show`, which declines
    a movie-framed ask and anything the catalogue does not really hold. Checked
    after the app pages and before the knowledge fallback (see resolve_display).
    """
    try:
        from puffpai.books.show import resolve_book as _resolve
        return _resolve(query, client_id=client_id)
    except Exception:                              # pragma: no cover - guard
        return None


def _manifest_path() -> str:
    """Path to indexed_libraries.json (the upload/ingestion manifest), which
    lives beside this module in src/puffpai/."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "indexed_libraries.json")


def _resolve_upload_collection(filename: str) -> str | None:
    """Map an uploaded document's filename to the knowledge-library collection
    the viewer opens (``?collection=``), via the ingestion manifest. Returns
    None if no upload entry matches. The manifest's ``library`` field is the
    original filename that ``search_indexed_docs`` returns as ``filename``, so an
    exact (case-insensitive) match is the primary path; a loose substring match
    is the fallback for minor transcription differences."""
    if not filename:
        return None
    try:
        with open(_manifest_path(), encoding="utf-8") as f:
            manifest = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(manifest, list):
        return None
    uploads = [e for e in manifest
               if isinstance(e, dict) and e.get("uploaded_at") and e.get("collection")]
    low = filename.strip().lower()
    for entry in uploads:
        if str(entry.get("library") or "").strip().lower() == low:
            return entry["collection"]
    for entry in uploads:  # loose fallback
        lib = str(entry.get("library") or "").strip().lower()
        if lib and (lib in low or low in lib):
            return entry["collection"]
    return None


def _search_upload_docs(topic: str) -> tuple | None:
    """Find the best-matching uploaded document by FILENAME against the topic.

    Returns ``(filename, collection)`` or None. The manifest is the source of
    truth for uploads: each per-document library records its original filename
    (``library``) and viewer id (``collection``). ``search_indexed_docs`` is a
    separate content index (a shared ChromaDB collection) that on this appliance
    does not contain these per-doc uploads, so it returns nothing for them — a
    filename match against the manifest is the reliable path and covers the
    common case where the user names the doc by its filename ("training sheet",
    "grandma's recipe"). Content search, when populated, is tried as a fallback
    by the caller.
    """
    words = [w for w in re.findall(r"[a-z0-9]+", (topic or "").lower()) if len(w) >= 3]
    if not words:
        return None
    try:
        with open(_manifest_path(), encoding="utf-8") as f:
            manifest = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(manifest, list):
        return None
    best, best_score = None, 0
    for entry in manifest:
        if not (isinstance(entry, dict) and entry.get("uploaded_at") and entry.get("collection")):
            continue
        name_low = str(entry.get("library") or "").lower()
        score = sum(1 for w in words if w in name_low)
        if score > best_score:
            best, best_score = (str(entry.get("library") or ""), entry["collection"]), score
    return best if best_score > 0 else None


def resolve_display(query: str) -> dict | None:
    """Resolve a "show me X" request to a display action, routed by source.

    Returns one of:
      * a ready display dict ``{kind, url, title, …}`` (knowledge/document), OR
      * ``{"deferred": "vault"|"document", "title": …}`` — recognized but not
        pulled up here (the vault flow and the document viewer are their own
        pieces; see the spun-off vault project and the frontend viewer), OR
      * ``None`` when nothing matched.
    """
    # App / dashboard pages first: "pull up my calendar" is navigation to one of
    # the user's own pages, not a knowledge/document search.
    app = resolve_app_page(query)
    if app:
        return app

    source = classify_source(query)
    topic = _extract_topic(query)
    if not topic or len(topic) < 2:
        return None

    # VAULT: recognize by kind only. NEVER search or index the vault here.
    if source == "vault":
        return {"deferred": "vault", "title": topic}

    # DOCUMENTS: the user's own uploads. Search them, map the best hit to its
    # knowledge-library collection, and pull it up in the same viewer the
    # knowledge shelves use — which renders an uploaded doc through SourceView
    # (PDF/image embed, or reconstructed text). Falls back to the spoken
    # "found it but can't show it" note only if the collection can't be mapped.
    if source == "documents":
        from urllib.parse import quote
        # Primary: filename match against the manifest (reliable — see
        # _search_upload_docs). Fallback: content search, for when the shared
        # ChromaDB index actually contains the uploads.
        hit = _search_upload_docs(topic)
        if not hit:
            try:
                from puffpai.ingestion import search_indexed_docs
                docs = [h for h in search_indexed_docs(topic, max_results=3)
                        if h.get("score", 0) >= 0.35]
            except Exception:
                docs = []
            if docs:
                fn = docs[0].get("filename") or docs[0].get("title") or topic
                col = _resolve_upload_collection(fn)
                hit = (fn, col) if col else None
        if hit:
            filename, collection = hit
            url = f"/dashboard/knowledge/viewer?collection={quote(collection)}"
            return {"kind": KIND_KNOWLEDGE, "url": url, "title": filename,
                    "topic": topic}
        return None

    # BOOKS: the household's own Calibre catalogue, ahead of the generic
    # knowledge fallback — "show me Moby Dick" is a book we HAVE, not a
    # Wikipedia article about one. Declines a movie-framed ask outright.
    book = resolve_book(query)
    if book:
        return book

    # KNOWLEDGE: the offline ZIM shelves — this one renders today.
    prefer = None
    low = query.lower()
    if re.search(r"\b(ted|talk|video|lecture|clip)\b", low):
        prefer = "ted"
    elif re.search(r"\b(wikipedia|wiki|encyclopedia)\b", low):
        prefer = "wikipedia"
    try:
        from puffpai.knowledge import fts_index
        searcher = fts_index.get_searcher()
        ready = searcher.ready_library_ids()
        libs = [l for l in ready if l.startswith(prefer)] if prefer else None
        res = searcher.search(topic, max_results=5, libraries=libs or None)
        if not res and libs:
            res = searcher.search(topic, max_results=5)  # widen if shelf missed
    except Exception:
        return None
    if not res:
        return None
    top = res[0]
    from urllib.parse import quote
    # Video shelves (TED today) render as a native <video> in the viewer. Tell
    # it so explicitly with ?kind=video instead of leaving it to guess from the
    # collection name — the guess (startsWith "ted") is the fallback, this is
    # the contract. Any future video library just needs to be recognized here.
    is_video = str(top.get("library_id", "")).lower().startswith("ted")
    # Point at the dashboard's existing knowledge viewer, which already renders
    # ZIM articles AND TED-talk videos (getArticle + video/subtitle detection),
    # deep-linked (?article=) to this exact hit. Same-origin path: the kiosk
    # ShowViewer overlay iframes it and the viewer fetches the article itself
    # (auth handled there), so no cross-origin/asset-rewrite work is needed here.
    url = (f"/dashboard/knowledge/viewer?collection={quote(top['library_id'])}"
           f"&article={quote(top['path'])}" + ("&kind=video" if is_video else ""))
    return {"kind": KIND_KNOWLEDGE, "url": url,
            "title": top.get("title") or topic,
            "library_id": top["library_id"], "article": top["path"],
            "topic": topic}


# Spoken replies for the recognized-but-not-yet-rendered cases.
DEFER_VAULT_MSG = (
    "That sounds like something you'd keep in your private vault. I can pull "
    "protected documents up with your PIN, but secure on-screen vault viewing "
    "is a separate feature I'm building carefully — I won't show sensitive "
    "documents until it's done right.")
DEFER_DOCUMENT_MSG = (
    "I found \"{title}\" in your uploads. Pulling your own documents up on "
    "screen is the last piece I'm wiring — for now I can read it to you.")

# The Zone-2 challenge spoken when a sensitive doc-display ask is recognized.
# It NEVER confirms anything exists in the vault — it just asks for the PIN.
VAULT_CHALLENGE_MSG = (
    "That sounds like something in your private vault. What's your PIN and "
    "I'll pull it up.")


# ── Vault owner + doc_type normalization (shared table, longest-phrase-first) ─
# The SAME phrase→canonical map tags a doc at store time and matches a spoken
# query at display time, so the tagger and the matcher can NEVER drift. Ordered
# longest-phrase-first so "social security card" wins before "social security"
# and "driver's license" before "license". Canonical doc_type values are the
# enum in security/vault.py's STRICT/HEALTH/FINANCIAL tiers (+ baseline).

# phrase (lowercase) → canonical doc_type
_DOC_TYPE_MAP = {
    "social security card": "ssn",
    "social security number": "ssn",
    "social security": "ssn",
    "ssn": "ssn",
    "passport": "passport",
    "driver's license": "drivers_license",
    "drivers license": "drivers_license",
    "driving license": "drivers_license",
    "driver license": "drivers_license",
    "state identification": "state_id",
    "state id card": "state_id",
    "state id": "state_id",
    "id card": "state_id",
    "bank account": "bank_account",
    "account number": "bank_account",
    "routing number": "bank_account",
    "medical record": "medical_record",
    "health record": "medical_record",
    "prescription": "prescription",
    "immunization record": "immunization_record",
    "vaccination record": "immunization_record",
    "vaccine record": "immunization_record",
    "immunization": "immunization_record",
    "insurance card": "insurance_card",
    "insurance policy": "insurance_card",
    "tax return": "tax_return",
    "w-2": "tax_return",
    "w2": "tax_return",
    "1099": "tax_return",
    "bank statement": "bank_statement",
    # Certificates / legal docs — DISTINCT BASELINE canonical types (90s TTL), so
    # two docs for one owner (e.g. a birth certificate AND a will) are told apart
    # by type. `other_vault_doc` stays reserved as the STRICT catch-all for an
    # explicitly-stored misc sensitive doc — it is intentionally NOT produced by
    # any spoken phrase here (an unrecognized vault phrase fails closed instead).
    "birth certificate": "birth_certificate",
    "death certificate": "death_certificate",
    "marriage certificate": "marriage_certificate",
    "marriage license": "marriage_certificate",
    "living will": "will",
    "last will": "will",
    "will": "will",
    "title deed": "deed",
    "deed": "deed",
    "mortgage": "mortgage",
}
# Longest phrase first so a more specific phrase wins the substring scan.
_DOC_TYPE_PHRASES = tuple(sorted(_DOC_TYPE_MAP.keys(), key=len, reverse=True))
# Every individual word that appears in a doc-type phrase — used to reject a
# possessive that is actually part of the type ("driver's license" must NOT
# read "driver" as the owner).
_DOC_TYPE_WORDS = {w for phrase in _DOC_TYPE_MAP for w in phrase.split()}

# Bare relationship owners (no possessive required).
_RELATIONSHIP_OWNERS = {
    "grandma", "grandmother", "grandpa", "grandfather", "mom", "mum",
    "mother", "dad", "father", "wife", "husband", "son", "daughter",
    "sister", "brother", "partner", "spouse",
}
_SELF_OWNERS = {"my", "mine", "our", "ours", "me", "i"}


def normalize_doc_type(phrase: str) -> str | None:
    """Map a spoken phrase to a canonical doc_type, or None. Longest-first so
    the most specific phrase wins (never a substring cross between types)."""
    low = (phrase or "").lower()
    for p in _DOC_TYPE_PHRASES:
        if p in low:
            return _DOC_TYPE_MAP[p]
    return None


def normalize_owner(phrase: str) -> str | None:
    """Extract a canonical owner token from a spoken phrase, or None.

    A possessive name ("<a name>'s" → that name) or relationship ("grandma's" →
    "grandma") wins first; then an explicit self-possessive ("my"/"our" →
    "self"); then a bare relationship word. Returns None when no owner is named
    (the caller then matches by type across owners and disambiguates)."""
    low = (phrase or "").lower()
    # possessive tokens: <a name>'s, grandma's (accept straight and curly apostrophe)
    for m in re.finditer(r"\b([a-z]+)['’]s\b", low):
        w = m.group(1)
        if w in _DOC_TYPE_WORDS:
            continue  # "driver's" in "driver's license" is not an owner
        if w in _SELF_OWNERS:
            return "self"
        return w
    # explicit self-possessive determiner
    for w in re.findall(r"\b([a-z]+)\b", low):
        if w in _SELF_OWNERS:
            return "self"
    # bare relationship word
    for rel in _RELATIONSHIP_OWNERS:
        if re.search(r"\b" + re.escape(rel) + r"\b", low):
            return rel
    return None


def normalize_vault_query(phrase: str) -> tuple:
    """Normalize a spoken query phrase to ``(owner_token, doc_type)``. Either
    may be None. Shared by the Zone-1 matcher (Piece D)."""
    return normalize_owner(phrase), normalize_doc_type(phrase)


def show_query(query: str, source: str = "voice") -> dict | None:
    """Resolve a "show me X" ask AND pull it up. Returns the set display item,
    or a ``{"deferred": …, "message": …}`` dict for a recognized-but-not-
    rendered case, or None if nothing matched. Shared by the fastpath + tool.
    """
    hit = resolve_display(query)
    if not hit:
        return None
    if hit.get("deferred") == "vault":
        # Zone-2 challenge-response: stash ONLY the query phrase and speak the
        # PIN challenge. The PIN arrives next turn via the api_server fastpath;
        # no vault lookup happens here and nothing sensitive is emitted.
        set_pending_vault(hit["title"])
        return {"deferred": "vault_challenge", "message": VAULT_CHALLENGE_MSG}
    if hit.get("deferred") == "document":
        return {"deferred": "document",
                "message": DEFER_DOCUMENT_MSG.format(title=hit["title"])}
    return set_display(hit["kind"], hit["url"], hit["title"], source=source,
                       meta=hit.get("meta") or {
                           "library_id": hit.get("library_id"),
                           "article": hit.get("article"),
                           "topic": hit.get("topic")})


__all__ = ["get_display", "get_state", "set_display", "clear_display",
           "resolve_display", "resolve_app_page", "show_query",
           "set_ephemeral_display", "clear_ephemeral_display", "ephemeral_active",
           "set_pending_vault", "pending_vault_active", "take_pending_vault",
           "clear_pending_vault",
           "normalize_owner", "normalize_doc_type", "normalize_vault_query",
           "VAULT_CHALLENGE_MSG",
           "KIND_KNOWLEDGE", "KIND_URL", "KIND_VAULT", "KINDS"]
