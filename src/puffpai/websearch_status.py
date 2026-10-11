"""Is web search actually working right now — asked honestly, answered cheaply.

Why this module exists
----------------------
"Can you search the web?" must be answered from the live switches. A
**static** ``status=NEEDS_AUTH`` on ``utility.websearch`` in
``capability_registry`` would append "(needs a one-time setup first)" to every
self-description while ``chat_allow_egress`` is on, the ``web_search`` and
``web_fetch`` doors are open, and the chat fastpath is answering live queries.

That is the same class of defect the probes module is built for, pointed the
other way. ``capability_probes`` stops PuffPAI OVER-claiming. This one stops
it UNDER-claiming, and an appliance that
tells the owner it cannot do something it is doing is not being modest, it is
being wrong about its own reach. The owner cannot audit egress she has been
told is not happening.

Four states, because the box really has four
--------------------------------------------
``OFF``          the owner's switches say no — the opt-in, or both doors.
``UNREACHABLE``  the switches say yes, the SearXNG container does not answer.
``DEGRADED``     SearXNG answers, but recent searches came back with nothing.
``LIVE``         switches on, SearXNG answering, no recent empty search.

``DEGRADED`` is not decoration. A search engine can answer every SearXNG
request with its anti-scraping CAPTCHA page, so the search box is up and every
search returns zero results. "Web search is on" would be true and useless;
"web search is off" would be false. The owner needs the third sentence.

The engine in use can change. Rather than hard-code a name,
:func:`engine_name` reads it live from SearXNG's own ``/config``, so the
sentence the owner hears always matches ``keep_only`` on the box, not
whatever engine happened to be true the day this file was last edited.

Two properties this module is responsible for
---------------------------------------------
1. **Asking the question must not itself be egress.** ``describe()`` runs on
   dashboard polls and on any "what can you do" turn. So the reachability
   probe is a loopback ``GET /healthz`` against the OWNER-CONFIGURED SearXNG
   address, and it is skipped entirely unless that address is the loopback
   pinhole (:func:`_is_loopback_base`) — a config that points somewhere public
   must not turn a capability question into an outbound request. Proxies are
   explicitly disabled on the opener for the same reason: an ``http_proxy`` in
   the environment would otherwise route a "loopback" probe off the box.
2. **Knowing that searches are coming back empty must not cost a search.**
   Nothing here queries an upstream engine. ``DEGRADED`` is derived from
   :func:`note_search_outcome`, which the search path calls with the result
   count it *already* obtained. Observations are in-memory and expire
   (:data:`OUTCOME_TTL_SECONDS`), so a stale opinion cannot outlive the fact.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# ── the four states ──────────────────────────────────────────────────────────

OFF = "off"
UNREACHABLE = "unreachable"
DEGRADED = "degraded"
LIVE = "live"

#: What to say when the live engine name cannot be read (SearXNG unreachable,
#: bad response, or no cached value yet). Never a guessed engine name — a
#: wrong engine name would be exactly the drift :func:`engine_name` exists to
#: prevent.
FALLBACK_ENGINE_NAME = "the configured search engine"

#: How long a ``/config`` engine-name answer is reused. Long enough that a
#: capability question does not open a socket per turn; short enough that an
#: owner who changes the engine sees the new name within minutes.
ENGINE_NAME_TTL_SECONDS = 300.0

#: Human names for engine ids SearXNG might report as enabled. Unknown ids
#: fall back to a title-cased version of the id itself.
_ENGINE_HUMAN_NAMES = {
    "duckduckgo": "DuckDuckGo",
    "brave": "Brave Search",
    "mojeek": "Mojeek",
    "startpage": "Startpage",
}

#: How long a ``/healthz`` answer is reused. Long enough that a dashboard poll
#: loop does not open a socket per capability row; short enough that restarting
#: the container shows up within a minute.
PROBE_TTL_SECONDS = 60.0

#: How long the probe waits. A capability question is a conversational turn —
#: a second is already the outer bound of what is acceptable to block it for.
PROBE_TIMEOUT_SECONDS = 1.0

#: How long a search outcome is allowed to speak for the present. Past this,
#: "the last search came back empty" is history, not a status.
OUTCOME_TTL_SECONDS = 1800.0

#: The note appended to the registry row when the owner's switches say no.
#: Matches ``capability_registry.EGRESS_OFF_NOTE`` in shape on purpose.
NOTE_OFF = "off — Settings > Egress"
NOTE_UNREACHABLE = "search service unreachable"


# ── observed search outcomes (no network) ────────────────────────────────────

_outcome_lock = threading.Lock()
#: ``(recorded_at_monotonic, had_results)`` or ``None``.
_last_outcome: "tuple[float, bool] | None" = None


def note_search_outcome(result_count: int) -> None:
    """Record what the LAST real search returned. Never raises.

    Called by the search path with a count it already has, so the degraded
    state costs no extra query. A successful search clears a previous empty
    one immediately — evidence of working beats evidence of broken, because
    the broken evidence is by definition older.
    """
    global _last_outcome
    try:
        count = int(result_count or 0)
    except (TypeError, ValueError):
        return
    with _outcome_lock:
        _last_outcome = (time.monotonic(), count > 0)


def _recent_outcome() -> "bool | None":
    """``True``/``False`` for the last non-stale search, else ``None``."""
    with _outcome_lock:
        hit = _last_outcome
    if hit is None:
        return None
    recorded_at, had_results = hit
    if time.monotonic() - recorded_at > OUTCOME_TTL_SECONDS:
        return None
    return had_results


def reset_outcomes() -> None:
    """Forget every observation. For tests and for a config reload."""
    global _last_outcome
    with _outcome_lock:
        _last_outcome = None


# ── the loopback reachability probe ──────────────────────────────────────────

_probe_lock = threading.Lock()
#: ``(expires_at_monotonic, reachable)`` or ``None``.
_probe_cache: "tuple[float, bool] | None" = None


def _searxng_base() -> str:
    """The owner-configured SearXNG base URL, or the loopback default."""
    try:
        from puffpai.agents import toolsmith
        default = toolsmith._DEFAULT_SEARXNG_URL
    except Exception:  # pragma: no cover - toolsmith always ships with it
        default = "http://127.0.0.1:8888"
    try:
        from puffpai.config import get_config
        configured = getattr(get_config().tools, "searxng_url", "") or ""
    except Exception as exc:
        logger.debug("websearch-status: config unreadable: %s", exc)
        configured = ""
    return (str(configured).strip() or default).rstrip("/")


def _is_loopback_base(base: str) -> bool:
    """Is *base* the loopback pinhole the Toolsmith already allows?

    Reuses the Toolsmith's own predicate rather than re-implementing the host
    comparison: two copies of an address check are two chances to disagree,
    and this one is deciding whether a *status question* is allowed to open a
    socket at all.
    """
    try:
        from puffpai.agents import toolsmith
        return bool(toolsmith._is_searxng_loopback(str(base or "")))
    except Exception as exc:  # pragma: no cover - import guard
        logger.debug("websearch-status: loopback check unavailable: %s", exc)
        return False


def _healthz(base: str) -> bool:
    """One ``GET /healthz``. Proxy-free, redirect-free, 1 s. Never raises."""
    # A bare ProxyHandler({}) is the point: with http_proxy set in the unit's
    # environment, urlopen would send this "loopback" probe to the proxy, which
    # would make answering "can you search the web" an outbound request.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
    )
    request = urllib.request.Request(
        f"{base}/healthz", method="GET",
        headers={"User-Agent": "PuffPAI-status/1.0", "Accept": "*/*"})
    try:
        with opener.open(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
            return 200 <= int(response.status) < 300
    except Exception as exc:
        logger.debug("websearch-status: searxng healthz failed: %s",
                     type(exc).__name__)
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx from the search box is an answer, not a new destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_searxng() -> bool:
    """Is the SearXNG container answering? Cached :data:`PROBE_TTL_SECONDS`.

    A non-loopback configured address returns ``True`` WITHOUT probing: the
    address is owner-fixed and the Toolsmith re-validates it before any real
    search, and probing it from here would be egress on a dashboard poll. This
    function's job is to catch "the container is down", not to police the
    config.
    """
    global _probe_cache
    now = time.monotonic()
    with _probe_lock:
        hit = _probe_cache
        if hit is not None and hit[0] > now:
            return hit[1]

    base = _searxng_base()
    reachable = _healthz(base) if _is_loopback_base(base) else True

    with _probe_lock:
        _probe_cache = (time.monotonic() + PROBE_TTL_SECONDS, reachable)
    return reachable


def reset_probe_cache() -> None:
    """Drop the cached reachability answer. For tests and for a restart."""
    global _probe_cache
    with _probe_lock:
        _probe_cache = None


# ── which engine is actually enabled ─────────────────────────────────────────

_engine_name_lock = threading.Lock()
#: ``(expires_at_monotonic, name)`` or ``None`` — the last name we managed to
#: read, kept past its TTL as the fallback-of-last-resort so a momentary
#: ``/config`` hiccup does not make the sentence regress to a generic phrase.
_engine_name_cache: "tuple[float, str] | None" = None


def _humanize_engine_id(engine_id: str) -> str:
    key = str(engine_id or "").strip().lower()
    return _ENGINE_HUMAN_NAMES.get(key, key.replace("_", " ").title() or key)


def _fetch_enabled_engine_names(base: str) -> "list[str] | None":
    """``GET /config`` and return the enabled engines' human names, or
    ``None`` on any failure. Proxy-free, redirect-free, 1 s. Never raises."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
    )
    request = urllib.request.Request(
        f"{base}/config", method="GET",
        headers={"User-Agent": "PuffPAI-status/1.0", "Accept": "application/json"})
    try:
        with opener.open(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
            if not (200 <= int(response.status) < 300):
                return None
            import json
            data = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:
        logger.debug("websearch-status: searxng config failed: %s",
                     type(exc).__name__)
        return None

    try:
        engines = data.get("engines") or []
        names = [
            _humanize_engine_id(engine.get("name") or engine.get("id") or "")
            for engine in engines
            if isinstance(engine, dict) and engine.get("enabled", True)
        ]
        names = [name for name in names if name]
    except Exception as exc:
        logger.debug("websearch-status: searxng config unparseable: %s", exc)
        return None
    return names or None


def engine_name() -> str:
    """The human name(s) of the engine(s) SearXNG currently has enabled.

    Reads the owner-configured SearXNG's own ``/config`` live rather than
    hard-coding a name, so this sentence tracks ``keep_only`` on the box
    when the engine is switched (DuckDuckGo -> Brave Search, say). Cached
    :data:`ENGINE_NAME_TTL_SECONDS`; on any failure, the last name this
    function managed to read, however stale; if it has never once succeeded,
    :data:`FALLBACK_ENGINE_NAME`.

    Same loopback discipline as :func:`probe_searxng`: a non-loopback
    configured address is not queried from here, because a status question
    must not itself be egress.
    """
    global _engine_name_cache
    now = time.monotonic()
    with _engine_name_lock:
        hit = _engine_name_cache
        if hit is not None and hit[0] > now:
            return hit[1]

    base = _searxng_base()
    names = _fetch_enabled_engine_names(base) if _is_loopback_base(base) else None

    if names:
        name = " and ".join(names)
        with _engine_name_lock:
            _engine_name_cache = (time.monotonic() + ENGINE_NAME_TTL_SECONDS, name)
        return name

    with _engine_name_lock:
        stale = _engine_name_cache
    if stale is not None:
        return stale[1]
    return FALLBACK_ENGINE_NAME


def reset_engine_name_cache() -> None:
    """Drop the cached engine name. For tests and for a restart."""
    global _engine_name_cache
    with _engine_name_lock:
        _engine_name_cache = None


def reset() -> None:
    """Drop every cached opinion this module holds."""
    reset_probe_cache()
    reset_outcomes()
    reset_engine_name_cache()


# ── the switches ─────────────────────────────────────────────────────────────

def _switches() -> "tuple[bool, str]":
    """``(allowed, reason)`` for a CHAT turn going online. Never raises.

    Deliberately the same gate a real chat search passes through
    (``egress_policy.chat_egress_allowed``), not the doors read separately:
    a status that disagrees with the gate deciding the same turn is exactly
    the defect this module exists to prevent.

    Fails CLOSED. An unreadable policy is not consent, and an appliance that
    says "web search is on" while the gate would refuse is back to lying, just
    in the other direction.
    """
    try:
        from puffpai.security import egress_policy
        return egress_policy.chat_egress_allowed("web_search")
    except Exception as exc:
        logger.warning("websearch-status: egress policy unreadable: %s", exc)
        return False, f"egress policy unreadable ({type(exc).__name__})"


# ── the answer ───────────────────────────────────────────────────────────────

def availability() -> "tuple[str, str]":
    """``(state, reason)`` — one of :data:`OFF` / :data:`UNREACHABLE` /
    :data:`DEGRADED` / :data:`LIVE`.

    Order matters and is the order of authority: the owner's switch outranks
    the container's health, which outranks what the last search happened to
    return. A shut door is not "unreachable" — nothing was even tried.
    """
    allowed, reason = _switches()
    if not allowed:
        return OFF, reason
    if not probe_searxng():
        return UNREACHABLE, NOTE_UNREACHABLE
    if _recent_outcome() is False:
        return DEGRADED, note_degraded()
    return LIVE, ""


def note_degraded() -> str:
    """The reason string for :data:`DEGRADED`, naming the live engine."""
    return f"search service is up, but {engine_name()} is refusing our searches"


def registry_note() -> str:
    """The parenthetical for the ``utility.websearch`` row, ``""`` when live.

    Mirrors ``capability_registry``'s house style: a leading space and a
    parenthesis, so it appends straight onto a spoken label.
    """
    state, _reason = availability()
    if state == LIVE:
        return ""
    return " (" + {OFF: NOTE_OFF,
                   UNREACHABLE: NOTE_UNREACHABLE,
                   DEGRADED: note_degraded()}[state] + ")"


def probe_capability() -> "tuple[bool, str]":
    """``(available, reason)`` in the shape ``capability_probes`` expects.

    ``DEGRADED`` reports **available** with a reason attached rather than
    unavailable: web search genuinely is switched on and wired up, and calling
    the whole capability offline because an upstream engine is bot-blocking us
    today would trade one wrong answer for another — the same judgement
    ``capability_probes`` already made for ``security.network``'s read half.
    """
    state, reason = availability()
    if state == LIVE:
        return True, ""
    if state == DEGRADED:
        return True, (
            f"Web search is on, but {engine_name()} — the only search engine "
            "this appliance is allowed to use — is refusing our searches "
            "right now, so recent lookups have come back empty.")
    if state == UNREACHABLE:
        return False, (
            "The search service on this box is not answering, so I cannot "
            "look anything up on the web until it is back.")
    return False, (
        "Web search is switched off for chat turns right now "
        f"(Settings > Egress). {reason}" if reason else
        "Web search is switched off for chat turns right now "
        "(Settings > Egress).")


def _answer_live() -> str:
    """The sentence for the LIVE case."""
    return (
        "Yes — web search is on. I use your own SearXNG search box with "
        f"{engine_name()} only, and every lookup is written to the egress log.")


def _answer_degraded() -> str:
    return (
        "Web search is switched on and your own SearXNG search box is running, "
        f"but {engine_name()} — the only engine I am allowed to query — is "
        "refusing our searches right now, so my recent lookups have come back "
        "empty. Nothing is being answered from memory in their place, and "
        "every attempt is still written to the egress log.")


ANSWER_UNREACHABLE = (
    "Not right now. Web search is switched on, but your own SearXNG search "
    "box on this box is not answering, so I have no way to look anything up "
    "until it is back. I am not going to answer from memory instead.")


def _answer_off() -> str:
    return (
        "No — web search is off right now, so nothing I say comes from the "
        "internet. You can turn it on in Settings > Egress: chat turns need "
        "'chat_allow_egress' and the 'web_search' door both on. When it is on "
        f"I use your own SearXNG search box with {engine_name()} only, and "
        "every lookup is written to the egress log.")


def capability_answer() -> str:
    """The deterministic reply to "can you search the web at all?".

    No model is consulted, on purpose: this is the one question whose wrong
    answer is self-concealing. A model asked whether it can browse answers
    from its training data, which is how the owner came to believe the box
    could not search while it was searching.
    """
    state, _reason = availability()
    return {LIVE: _answer_live,
            DEGRADED: _answer_degraded,
            UNREACHABLE: lambda: ANSWER_UNREACHABLE,
            OFF: _answer_off}[state]()


#: Function aliases matching the ``ANSWER_*`` naming other modules import by.
#: Not plain strings anymore — the engine name they contain can change while
#: the process runs, so a pinned-at-import-time constant would go stale the
#: same way ``ENGINE_NAME`` did. Callers must call these, not read them.
answer_live = _answer_live
answer_degraded = _answer_degraded
answer_off = _answer_off


__all__ = [
    "ANSWER_UNREACHABLE",
    "DEGRADED",
    "ENGINE_NAME_TTL_SECONDS",
    "FALLBACK_ENGINE_NAME",
    "LIVE",
    "NOTE_OFF",
    "NOTE_UNREACHABLE",
    "OFF",
    "OUTCOME_TTL_SECONDS",
    "PROBE_TIMEOUT_SECONDS",
    "PROBE_TTL_SECONDS",
    "UNREACHABLE",
    "answer_degraded",
    "answer_live",
    "answer_off",
    "availability",
    "capability_answer",
    "engine_name",
    "note_degraded",
    "note_search_outcome",
    "probe_capability",
    "probe_searxng",
    "registry_note",
    "reset",
    "reset_engine_name_cache",
    "reset_outcomes",
    "reset_probe_cache",
]
