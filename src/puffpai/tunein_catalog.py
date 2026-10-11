"""TuneIn (Radio Time) as a SECOND station catalog for the appliance player.

WHY A SECOND CATALOG
    radio-browser is a volunteer database. It is excellent at genres and at
    internationally known streams, and weak exactly where the household asks
    most often: the local AM/FM call letters. "play KEXP" / "play WNYC" /
    "play 1010 WINS" comes back empty often enough that she was told "no
    station matched" for stations that plainly exist. TuneIn indexes those by
    call sign, so it is consulted when a NAME query finds nothing and merged
    into the candidate list otherwise.

THE OPML API (the same two calls the dashboard's TuneIn route already makes)
    Search   ``opml.radiotime.com/Search.ashx?query=..&types=station&render=json``
             -> ``{"body": [ {type: "audio", guide_id: "s12345", text: ...} ]}``
             Only rows with ``type == "audio"`` and a ``s<digits>`` guide id are
             stations; everything else is a folder/topic/show.
    Resolve  ``opml.radiotime.com/Tune.ashx?id=s12345&render=json``
             -> ``{"body": [ {url: "http://…"} ]}`` where the url is EITHER a
             direct stream or a ``.pls``/``.m3u`` playlist that has to be
             fetched and unwrapped (``File1=`` / the first http line).

EGRESS
    ``opml.radiotime.com`` is the sole host of the ``tunein`` door — TuneIn's
    OWN switch, separate from ``radio_browser``. radio-browser is a volunteer database; TuneIn is a commercial
    company, so one "yes" does not cover both. There is no ungated path: every
    request here goes through :func:`_get`, which refuses before it reaches
    ``egress_doors.egress_get`` when the door is shut, so a shut door returns
    nothing rather than reaching the internet.

    Callers ask :func:`door_open` when they need to tell "TuneIn had nothing"
    apart from "TuneIn was never asked" — the second is a sentence the
    household is owed, and the first must never be said in its place.

    THE DOOR IS CHECKED BEFORE THE CACHE, not after. A cache hit costs no
    egress, so serving one through a shut door looked harmless — it is not.
    The owner closing this switch and still being handed TuneIn rows for the
    next ten minutes, with the "I only looked on radio-browser" sentence
    suppressed because rows came back, is the appliance disagreeing with its
    own control panel. Closed means closed on the next call, not eventually.
    Cached entries are LEFT in place rather than dropped: they are public
    catalog rows, nothing is served from them while the door is shut, and
    reopening the switch should be cheap.

    KNOWN, DELIBERATE LIMITATION. A ``.pls``/``.m3u`` handed back by Tune.ashx
    lives on the BROADCASTER's server, which is not on any door's allowlist (and
    is usually plain http on a non-443 port). That fetch is therefore refused by
    the door and the candidate is skipped — fail closed. Unwrapping is
    implemented and tested because the parse is what makes a future allowance
    safe, but in the shipped configuration only stations whose Tune.ashx row is
    already a direct stream URL are playable from TuneIn.

NOTHING HERE RAISES. A catalog that is down, slow, or serving nonsense must
degrade to "no candidates", never to an exception in the middle of a play turn.

puff puff pass
"""
from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger(__name__)

_HOST = "opml.radiotime.com"
_SEARCH_URL = f"https://{_HOST}/Search.ashx"
_TUNE_URL = f"https://{_HOST}/Tune.ashx"

#: The egress door every request in this module needs. TuneIn's own switch,
#: NOT ``radio_browser`` — see the EGRESS note above.
DOOR_ID = "tunein"

#: Every call is inside a chat/voice turn, so the budget is small and fixed.
TUNEIN_TIMEOUT = 6
#: A playlist file is a handful of lines; anything larger is not a playlist.
MAX_PLAYLIST_BYTES = 64 * 1024
#: Search results are stable for far longer than this; 10 minutes keeps a
#: "play something else" burst off the network entirely.
CACHE_SECONDS = 600

_UA = "PuffPAI/1.0"

#: A station guide id. Shows ("p…"), topics ("t…") and folders ("c…") are not
#: tunable streams and must never reach the player.
_GUIDE_ID_RE = re.compile(r"^s\d+$")

#: TuneIn serves an HLS manifest for some stations; mpv can play it but the
#: dashboard's <audio> element cannot, and the two players share this list.
_HLS_RE = re.compile(r"\.m3u8(\?|$)", re.IGNORECASE)

#: The station-is-unavailable interstitial TuneIn hands back instead of an
#: error. Playing it produces a recorded apology, which reads as success.
_NOT_COMPATIBLE_RE = re.compile(r"notcompatible", re.IGNORECASE)

_cache: dict = {}


def _cache_get(key):
    hit = _cache.get(key)
    if not hit:
        return None
    at, value = hit
    if (time.monotonic() - at) > CACHE_SECONDS:
        _cache.pop(key, None)
        return None
    return value


#: The in-memory cache is unbounded no longer: a long-running box that searches
#: many stations would keep every result forever. Oldest insertion goes first.
CACHE_MAX_ENTRIES = 128

#: Hard ceiling on station rows examined per search, whatever the limit asks.
_MAX_ROWS_EXAMINED = 15


def _cache_put(key, value):
    if len(_cache) >= CACHE_MAX_ENTRIES:
        for stale in list(_cache)[:len(_cache) - CACHE_MAX_ENTRIES + 1]:
            _cache.pop(stale, None)
    _cache[key] = (time.monotonic(), value)
    return value


def clear_cache() -> None:
    """Drop the in-memory cache (tests; never needed at runtime)."""
    _cache.clear()


def door_open() -> bool:
    """Is the ``tunein`` door open? Cheap, no network, never raises.

    The cheap answer callers need in order to be HONEST: "radio-browser had
    nothing and TuneIn was not asked" is a different sentence from "neither
    had it", and only this tells them apart. Fails shut — an unreadable
    registry is not consent, and a caller that cannot tell must assume it was
    not searched.
    """
    try:
        from puffpai.security.egress_doors import egress_allowed
        return bool(egress_allowed(DOOR_ID)[0])
    except Exception as exc:                                  # pragma: no cover
        logger.info("tunein door state unreadable (%s) — treating as shut", exc)
        return False


def _get(url, params):
    """One gated GET. Returns the response, or None on any failure.

    The door decision runs FIRST, before the egress wrapper is even imported:
    a shut door must cost no DNS, no socket and no log line that reads like a
    network failure. ``egress_get`` would refuse too — this is the belt to its
    braces, and it is what makes "no egress call was attempted" testable.
    """
    if not door_open():
        logger.debug("tunein: door shut, not searching")
        return None
    from puffpai.security.egress_doors import egress_get
    try:
        resp = egress_get(
            DOOR_ID, url, params=params,
            headers={"User-Agent": _UA}, timeout=TUNEIN_TIMEOUT,
        )
        resp.raise_for_status()
        return resp
    except Exception as exc:
        logger.info("tunein request failed (%s): %s", url, exc)
        return None


def _rows(resp):
    try:
        payload = resp.json()
    except Exception:
        return []
    if isinstance(payload, dict):
        body = payload.get("body")
    elif isinstance(payload, list):
        body = payload
    else:
        body = None
    return [r for r in (body or []) if isinstance(r, dict)]


def _int(val) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _is_http(url) -> bool:
    return isinstance(url, str) and url.lower().startswith(("http://", "https://"))


def _playable(url) -> bool:
    return (_is_http(url) and not _HLS_RE.search(url)
            and not _NOT_COMPATIBLE_RE.search(url))


def _unwrap_playlist(text: str) -> str | None:
    """First playable http(s) URL in a .pls / .m3u body, or None.

    ``File1=`` wins when present (a .pls lists entries in order); otherwise the
    first bare http line. Comment lines (#EXTM3U, #EXTINF) are skipped.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and line.lower().startswith("file"):
            candidate = line.split("=", 1)[1].strip()
        elif _is_http(line):
            candidate = line
        else:
            continue
        if _playable(candidate):
            return candidate
    return None


def _looks_like_playlist(url: str) -> bool:
    return bool(re.search(r"\.(pls|m3u)(\?|$)", url or "", re.IGNORECASE))


def resolve_stream(guide_id: str) -> str | None:
    """Turn a TuneIn guide id into a directly playable stream URL.

    Returns None — never raises — when the id is not a station, the door is
    shut, the catalog is down, or every candidate is HLS / the "not compatible"
    clip / a non-http scheme.

    The door is asked BEFORE the cache: a shut door must not keep answering
    out of a warm cache for the rest of CACHE_SECONDS.
    """
    if not door_open():
        return None
    guide_id = (guide_id or "").strip()
    if not _GUIDE_ID_RE.match(guide_id):
        return None
    cached = _cache_get(("tune", guide_id))
    if cached is not None:
        return cached or None

    resp = _get(_TUNE_URL, {"id": guide_id, "render": "json"})
    if resp is None:
        return None
    for row in _rows(resp):
        url = row.get("url")
        if not _is_http(url):
            continue
        if _looks_like_playlist(url):
            inner = _get(url, None)
            if inner is None:
                continue
            try:
                body = (inner.content or b"")[:MAX_PLAYLIST_BYTES].decode(
                    "utf-8", "replace")
            except Exception:
                continue
            url = _unwrap_playlist(body)
        if _playable(url):
            return _cache_put(("tune", guide_id), url)
    _cache_put(("tune", guide_id), "")
    return None


def search_stations(query: str, limit: int = 8) -> list:
    """Search TuneIn by NAME / call sign. Returns radio-browser-shaped dicts.

    The shape is deliberately identical to :func:`media_hub._trim_station` so a
    TuneIn row and a radio-browser row are interchangeable everywhere
    downstream (ranking, the player, the mediahub state doc, favorites).
    ``source`` marks where it came from, for the report only.

    The door is asked BEFORE the cache, so closing the switch stops rows
    coming back on the very next call rather than when the cache expires.
    """
    if not door_open():
        return []
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit or 8), 20))
    cached = _cache_get(("search", query.lower(), limit))
    if cached is not None:
        return list(cached)

    resp = _get(_SEARCH_URL, {"query": query, "types": "station",
                              "render": "json"})
    if resp is None:
        return []

    out = []
    # Each surviving row costs a stream RESOLVE (a network round trip), so the
    # number examined is capped: a page of rows that never resolve must not
    # stall a voice turn while it tries all of them.
    examined = 0
    budget = min(limit * 3, _MAX_ROWS_EXAMINED)
    for row in _rows(resp):
        if str(row.get("type") or "").lower() != "audio":
            continue
        examined += 1
        if examined > budget:
            break
        guide_id = str(row.get("guide_id") or "").strip()
        if not _GUIDE_ID_RE.match(guide_id):
            continue
        name = str(row.get("text") or "").strip()
        if not name:
            continue
        url = resolve_stream(guide_id)
        if not url:
            continue
        out.append({
            "stationuuid": f"tunein:{guide_id}",
            "name": name,
            "url_resolved": url,
            "country": str(row.get("subtext") or "").strip(),
            "countrycode": "",
            "codec": str(row.get("formats") or "").strip().upper(),
            # TuneIn publishes a real bitrate on the search row, so ranking
            # compares like
            # with like. A row without one is "unmeasured", not "bad" — see
            # media_hub.rank_stations, which sorts a 0 from TuneIn above the
            # sub-64k rows rather than at the bottom.
            "bitrate": _int(row.get("bitrate")),
            "tags": str(row.get("genre_id") or "").strip(),
            "votes": 0,
            "homepage": "",
            "source": "tunein",
        })
        if len(out) >= limit:
            break
    _cache_put(("search", query.lower(), limit), list(out))
    return out
