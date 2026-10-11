"""
PuffPAI Media Hub — one state store for every audio source on the appliance.

The appliance plays audio from four places and only ONE of them (Jellyfin) has
a server we can ask. This module is the seam that makes the other three visible
to the dashboard's Now Playing tile and callable by the LLM:

    Jellyfin         media_bridge queries the server directly (not here).
    World Radio      the DASHBOARD owns the <audio> element, so the browser is
                     the only thing that knows whether sound is coming out.
                     It reports status here and polls for commands; the LLM
                     queues a command and the display picks it up. That
                     inversion is why there is a command queue at all.
    Spotify Connect  librespot pushes player events to us
                     (ONEVENT_POST_ENDPOINT -> /apps/mediahub/spotify-event).
                     We are a passive sink: there is no telemetry to pull and
                     no way to control a session that belongs to a phone.
All substantive logic lives here so api_server/assistant_tools stay thin shims.

puff puff pass
"""

import json
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Radio Browser ────────────────────────────────────────────────────────────
# Volunteer mirror pool; individual mirrors go dark regularly, so walk a list
# with a short timeout each. Same contract the dashboard's /api/radio/search
# proxy uses, so a station picked by the LLM is playable by the player.
_RADIO_MIRRORS = ["all.api.radio-browser.info", "de1.api.radio-browser.info"]
_RADIO_TIMEOUT = 6
_RADIO_UA = "PuffPAI/1.0"

# The only fields the player needs — everything else is dropped here.
_STATION_FIELDS = (
    "stationuuid", "name", "url_resolved", "country", "countrycode",
    "codec", "bitrate", "tags", "votes", "homepage",
)

VALID_RADIO_STATUS = ("idle", "connecting", "playing", "paused", "error")

# A display that stopped polling is a display that is not playing anything,
# whatever it last told us. Slightly over 2 poll intervals.
RADIO_STALE_SECONDS = 45

# A queued command the display never acked must die, not lie in wait: without
# this, a lost ack replays the command on the next kiosk reload — radio
# starting by itself hours later.
RADIO_COMMAND_TTL_SECONDS = 120

# Spotify state arrives only as librespot EVENTS (play/pause/track change) —
# there is no heartbeat, so a row can sit "active" forever after the phone
# walked away. Far longer than the radio gate because silence here is normal
# mid-album, but short enough that a row from weeks ago stops claiming the
# tile. A row with no readable updated_at is passed through unchanged: we can
# only call something stale if we can actually measure its age.
SPOTIFY_STALE_SECONDS = 600

MAX_PAYLOAD_BYTES = 16 * 1024
MAX_DEBUG_BYTES = 4 * 1024

# The poll heartbeat lands every 4s for the appliance's lifetime; persisting
# it would truncate+rewrite the JSON doc to flash on every tick (and a torn
# read could silently drop a pending command). api_server and assistant_tools run
# in one process, so a module variable is visible to both; the persisted
# radio_last_seen field (written by report/ack, including every server-side
# play) is both the cold-start fallback AND the signal for anything that
# changed the radio without a kiosk poll.
_radio_last_seen_iso = None


def media_log(detail):
    """One dispatch line per media turn, on STDOUT so it lands in the journal.

    A media request that logs the request and then NOTHING (no route line, no
    error, no trace of any decision, and no sound) is undiagnosable. Every
    media fastpath prints exactly one of these, for example
    ``radio stations query='jazz' -> 6 candidates`` or ``-> FAILED: <reason>``,
    success or failure.

    ``print``, not ``logger``, and deliberately so: the ``[Chat] Route:`` and
    ``[Chat] Capability read:`` lines this sits beside are prints, and the
    service installs no logging handlers, so a ``logger.info`` here would be
    invisible in exactly the situation this exists for.
    """
    try:
        print(f"[Media] {detail}")
    except Exception:                                     # pragma: no cover
        pass


def touch_radio_last_seen():
    global _radio_last_seen_iso
    _radio_last_seen_iso = _now_iso()


def _radio_last_seen(doc):
    """The freshest heartbeat we have: in-process marker or persisted field,
    whichever is NEWER.

    Preferring the in-process value unconditionally hid every server-side
    play. Voice/chat ("play some jazz radio") writes a fresh persisted
    ``radio_last_seen`` but runs no kiosk poll, so a module marker left over
    from some earlier poll gated the freshly-started station out of the Now
    Playing tile as "stale". Neither source is authoritative on its own;
    newest wins.
    """
    persisted = doc.get("radio_last_seen") if isinstance(doc, dict) else None
    if not _radio_last_seen_iso:
        return persisted or None
    if not persisted:
        return _radio_last_seen_iso
    live_age = _age_seconds(_radio_last_seen_iso)
    file_age = _age_seconds(persisted)
    # An unparseable stamp is not evidence of freshness: prefer whichever one
    # we can actually read, and keep the in-process value if neither parses.
    if file_age is None:
        return _radio_last_seen_iso
    if live_age is None:
        return persisted
    return _radio_last_seen_iso if live_age <= file_age else persisted


# Probing the appliance player costs a `docker ps` spawn and the tile polls
# every 5s per viewer, so the answer is cached — well inside the 45s gate.
_APPLIANCE_PROBE_TTL_SECONDS = 10
_appliance_probe = {"at": None, "live": False}


def _appliance_radio_live():
    """True when the server-side mpv container is actually streaming.

    The staleness gate exists because the BROWSER player's state write goes
    silent when the kiosk dies and there is no other way to notice. The
    appliance player is different: we can just ask it. A voice-started station
    reports its state once, at play time, and nothing re-reports it every 20s
    the way the browser bridge does — so without this the tile showed the
    station for 45s and then hid one that was still audibly playing.

    Only consulted for state the appliance itself wrote (``source`` ==
    ``appliance``); a dashboard-owned row keeps the pure heartbeat gate.
    """
    import time
    now = time.monotonic()
    at = _appliance_probe["at"]
    if at is not None and (now - at) < _APPLIANCE_PROBE_TTL_SECONDS:
        return _appliance_probe["live"]
    try:
        from puffpai import radio_player
        live = bool(radio_player.is_playing())
    except Exception:
        live = False
    _appliance_probe["at"] = now
    _appliance_probe["live"] = live
    return live


def _as_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(iso_ts):
    """Seconds since *iso_ts*, or None if it is absent/unparseable."""
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(str(iso_ts))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


# ══════════════════════════════════════════════════════════════════════════════
#  RADIO SEARCH
# ══════════════════════════════════════════════════════════════════════════════

def _trim_station(raw):
    """Keep only the player-relevant fields, coercing types defensively."""
    out = {}
    for key in _STATION_FIELDS:
        val = raw.get(key)
        if key in ("bitrate", "votes"):
            try:
                out[key] = int(val or 0)
            except (TypeError, ValueError):
                out[key] = 0
        else:
            out[key] = str(val or "")
    return out


def _station_params(limit, **extra):
    """Shared radio-browser query params. ``extra`` carries the search axis."""
    params = {
        "limit": max(1, min(_as_int(limit, 20), 50)),
        "hidebroken": "true",
        "order": "votes",
        "reverse": "true",
    }
    params.update(extra)
    return params


def _station_search(params):
    """Run one catalog query across the mirrors. Returns trimmed stations.

    HLS streams and entries with no resolved URL are dropped: the dashboard
    player is a bare <audio> element, so an HLS manifest would just fail
    silently after we told the user we were tuning it.
    """
    from puffpai.security.egress_doors import EgressBlocked, egress_get

    headers = {"User-Agent": _RADIO_UA, "Accept": "application/json"}

    for host in _RADIO_MIRRORS:
        try:
            resp = egress_get(
                "radio_browser",
                f"https://{host}/json/stations/search",
                params=params, headers=headers, timeout=_RADIO_TIMEOUT,
            )
            resp.raise_for_status()
            rows = resp.json()
        except EgressBlocked:
            # The door is shut, and it will be shut for the next mirror too, so
            # do not retry. Return empty rather than raising: this function has
            # six callers, and an exception escaping into all of them is a much
            # larger change than this row needs. The user-facing honesty lives
            # in tool_radio_play, which checks the door BEFORE searching and
            # says which switch is off.
            return []
        except Exception:
            continue  # mirror is down — try the next one
        if not isinstance(rows, list):
            continue
        trimmed = [
            _trim_station(r) for r in rows
            if isinstance(r, dict)
            and r.get("url_resolved")
            # _as_int: mirrors have served hls as "true"/"1"/1 — a bad row
            # must not throw the whole search (and the tool with it).
            and _as_int(r.get("hls"), 0) != 1
        ]
        if trimmed:
            return trimmed
        # This mirror returned nothing — a real station (e.g. KEXP) can just be
        # missing or lagging on one mirror. Try the next before giving up. That
        # empty-result-no-retry gap is what made "play KEXP" flaky.
    return []


def search_stations(query, limit=20):
    """Search the radio-browser catalog by station NAME."""
    query = (query or "").strip()
    if not query:
        return []
    return _station_search(_station_params(limit, name=query))


def search_stations_by_tag(tag, limit=20):
    """Search by GENRE / mood tag instead of station name.

    Name search cannot answer "play some lo-fi relaxing music" — no station is
    literally called that, so the catalog returns nothing and the request would
    fall through to a Jellyfin *title* search and be told no such track exists.
    radio-browser indexes genre tags on a separate axis,
    and a vibe request is really a tag request. Callers try this only after the
    name search, so an actual station name still wins.
    """
    tag = (tag or "").strip()
    if not tag:
        return []
    return _station_search(_station_params(limit, tag=tag))


# ══════════════════════════════════════════════════════════════════════════════
#  STRUCTURED SEARCH + QUERY INTERPRETATION
# ══════════════════════════════════════════════════════════════════════════════
#
# THE PROBLEM THIS ANSWERS: a radio assistant that cannot find stations and
# keeps defaulting back to a few favourites. Both halves are structural:
#
#   * a catalog asked only two questions (name, then tag) collapses
#     "french jazz", "news from japan" and "something Irish" onto one axis and
#     loses the other one;
#   * picking ``max(bitrate)`` over the top hits as the winner is a
#     DETERMINISTIC function of the query. The same words returned the same
#     station forever, which is exactly what "the same few she likes" is.
#
# So: search on four axes, return a ranked CANDIDATE LIST rather than one row,
# and remember what was played for a query class so a repeat can land somewhere
# else on purpose.

#: Genre words the household actually says -> the radio-browser tag to search.
#: Modest on purpose: a tag that is not in the catalog returns nothing, and an
#: over-eager vocabulary turns "play the rock station in Little Rock" into a
#: genre query. Longest phrase wins (see :func:`interpret_radio_query`).
RADIO_TAG_WORDS = {
    "jazz": "jazz", "smooth jazz": "smooth jazz", "classical": "classical",
    "opera": "opera", "rock": "rock", "classic rock": "classic rock",
    "indie": "indie", "alternative": "alternative", "alt rock": "alternative",
    "pop": "pop", "hip hop": "hip hop", "hiphop": "hip hop", "rap": "hip hop",
    "country": "country", "bluegrass": "bluegrass", "blues": "blues",
    "reggae": "reggae", "ska": "ska", "salsa": "salsa", "latin": "latin",
    "electronic": "electronic", "techno": "techno", "house": "house",
    "ambient": "ambient", "lofi": "lofi", "lo-fi": "lofi", "lo fi": "lofi",
    "chillout": "chillout", "metal": "metal", "heavy metal": "metal",
    "punk": "punk", "folk": "folk", "gospel": "gospel", "christian": "christian",
    "news": "news", "talk": "talk", "sports": "sports", "oldies": "oldies",
    "80s": "80s", "eighties": "80s", "90s": "90s", "nineties": "90s",
    "70s": "70s", "seventies": "70s", "60s": "60s", "sixties": "60s",
    "soul": "soul", "funk": "funk", "disco": "disco", "rnb": "rnb",
    "r&b": "rnb", "kpop": "kpop", "k-pop": "kpop", "k pop": "kpop",
    "world": "world", "world music": "world", "afrobeat": "afrobeat",
    "bollywood": "bollywood", "dance": "dance", "edm": "edm",
    "instrumental": "instrumental", "soundtrack": "soundtrack",
    "podcast": "podcast", "comedy": "comedy",
}

#: Country words and demonyms -> the country name radio-browser indexes.
RADIO_COUNTRY_WORDS = {
    "united states": "The United States Of America", "usa": "The United States Of America",
    "us": "The United States Of America", "american": "The United States Of America",
    "uk": "The United Kingdom Of Great Britain And Northern Ireland",
    "britain": "The United Kingdom Of Great Britain And Northern Ireland",
    "british": "The United Kingdom Of Great Britain And Northern Ireland",
    "england": "The United Kingdom Of Great Britain And Northern Ireland",
    "english": "The United Kingdom Of Great Britain And Northern Ireland",
    "scotland": "The United Kingdom Of Great Britain And Northern Ireland",
    "scottish": "The United Kingdom Of Great Britain And Northern Ireland",
    "ireland": "Ireland", "irish": "Ireland",
    "france": "France", "french": "France",
    "germany": "Germany", "german": "Germany",
    "spain": "Spain", "spanish": "Spain",
    "italy": "Italy", "italian": "Italy",
    "portugal": "Portugal", "portuguese": "Portugal",
    "brazil": "Brazil", "brazilian": "Brazil",
    "mexico": "Mexico", "mexican": "Mexico",
    "argentina": "Argentina", "argentinian": "Argentina",
    "japan": "Japan", "japanese": "Japan",
    "korea": "South Korea", "korean": "South Korea",
    "china": "China", "chinese": "China",
    "india": "India", "indian": "India",
    "canada": "Canada", "canadian": "Canada",
    "australia": "Australia", "australian": "Australia",
    "netherlands": "The Netherlands", "dutch": "The Netherlands",
    "sweden": "Sweden", "swedish": "Sweden",
    "norway": "Norway", "norwegian": "Norway",
    "greece": "Greece", "greek": "Greece",
    "poland": "Poland", "polish": "Poland",
    "nigeria": "Nigeria", "nigerian": "Nigeria",
    "kenya": "Kenya", "kenyan": "Kenya",
    "jamaica": "Jamaica", "jamaican": "Jamaica",
    "cuba": "Cuba", "cuban": "Cuba",
    "iceland": "Iceland", "icelandic": "Iceland",
}

#: Languages, only ever reached through an explicit "in <language>" shape —
#: "spanish" on its own means Spain far more often than it means the language.
RADIO_LANGUAGE_WORDS = {
    "english": "english", "spanish": "spanish", "french": "french",
    "german": "german", "italian": "italian", "portuguese": "portuguese",
    "japanese": "japanese", "korean": "korean", "mandarin": "chinese",
    "chinese": "chinese", "hindi": "hindi", "arabic": "arabic",
    "russian": "russian", "dutch": "dutch", "swedish": "swedish",
    "greek": "greek", "polish": "polish",
}

#: Words that are request scaffolding, never part of a station name.
_QUERY_FILLER = {
    "play", "put", "on", "some", "a", "an", "the", "me", "my", "for", "please",
    "radio", "station", "stations", "music", "listen", "to", "find", "search",
    "get", "want", "id", "like", "something", "anything", "else", "different",
    "another", "one", "kind", "of", "there", "what", "whats", "which", "can",
    "you", "i", "we", "is", "are", "in", "from", "at", "channel", "stream",
    "tune", "tunein", "hear", "channels", "available", "good", "nice",
}

_ISO_CODE_RE = re.compile(r"^[a-z]{2}$")


def _phrase_hit(low, table):
    """Longest phrase from *table* present in *low* -> (phrase, value)."""
    best = None
    for phrase, value in table.items():
        if re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", low):
            if best is None or len(phrase) > len(best[0]):
                best = (phrase, value)
    return best


def interpret_radio_query(text):
    """Split a spoken radio request into the axes the catalog indexes.

    Returns a dict with any of ``name`` / ``tag`` / ``country`` / ``language``;
    absent keys mean "the user did not ask on that axis". Purely local — this
    makes no network call and is safe to run on text that may never be allowed
    to leave the house (the caller still decides that; see
    ``radio_cues.plan_station_query``).

        "french jazz"        -> {"tag": "jazz", "country": "France"}
        "news from japan"    -> {"tag": "news", "country": "Japan"}
        "kexp"               -> {"name": "kexp"}
        "jazz in spanish"    -> {"tag": "jazz", "language": "spanish"}
    """
    low = " ".join((text or "").lower().replace("-", " ").split())
    if not low:
        return {}
    out = {}
    consumed = low

    lang = re.search(r"\b(?:in|speaking|language)\s+([a-z]+)\b", low)
    if lang and lang.group(1) in RADIO_LANGUAGE_WORDS:
        out["language"] = RADIO_LANGUAGE_WORDS[lang.group(1)]
        consumed = consumed.replace(lang.group(0), " ")

    tag_hit = _phrase_hit(consumed, RADIO_TAG_WORDS)
    if tag_hit:
        out["tag"] = tag_hit[1]
        consumed = consumed.replace(tag_hit[0], " ")

    country_hit = _phrase_hit(consumed, RADIO_COUNTRY_WORDS)
    if country_hit:
        out["country"] = country_hit[1]
        consumed = consumed.replace(country_hit[0], " ")

    leftover = [w for w in re.split(r"[^\w&']+", consumed)
                if w and w not in _QUERY_FILLER]
    if leftover:
        # A bare two-letter leftover next to nothing else is an ISO country
        # code ("play radio from se"), not a station called "se".
        if (len(leftover) == 1 and _ISO_CODE_RE.match(leftover[0])
                and "country" not in out and not out.get("tag")):
            out["country"] = leftover[0].upper()
        else:
            out["name"] = " ".join(leftover)
    return out


def _country_params(country):
    """radio-browser takes either a full country name or a 2-letter code."""
    c = str(country or "").strip()
    if not c:
        return {}
    if len(c) == 2 and c.isalpha():
        return {"countrycode": c.upper()}
    return {"country": c}


def _station_key(station):
    name = " ".join(str(station.get("name") or "").lower().split())
    return name or str(station.get("url_resolved") or "").lower()


# ── genre relevance ──────────────────────────────────────────────────────────
#
# "play a jazz radio station" must not tune a station that merely lists jazz
# among many tags. The catalog's tag search is ordered by votes and matches
# loosely, and the most-voted row for tag=jazz can be a station carrying 20-odd
# tags (decades, easy listening, oldies, lounge, swing ... and "jazz"). Ranking
# by bitrate/votes alone puts it at the head of every genre it has stuffed
# into its tag list.
# Exact tag membership does NOT catch it — the stuffed row really does carry
# the tag — so relevance is measured as FOCUS: how much of the station's own
# description is about the genre that was asked for.

#: Ask the catalog for this many rows on a genre request before ranking down to
#: the shortlist, so clean stations exist to replace the stuffed ones.
RADIO_TAG_FETCH_LIMIT = 40

#: More tags than this and a station is describing everything, not something.
_TAG_SPAM_COUNT = 8
#: ... unless at least this share of those tags is about the requested genre
#: (a jazz station listing twenty jazz sub-genres is thorough, not stuffed).
_TAG_FOCUS_SHARE = 0.25
#: This many tags from OTHER genres of the closed vocabulary is a grab-bag.
_TAG_FOREIGN_MAX = 3
#: Stuffed rows are dropped outright once this many clean ones exist; below
#: it they are kept, last, so a thin genre still plays something.
_CLEAN_ENOUGH = 3

#: Fallback "tags" that are not a genre; ranking ignores them.
_GENERIC_TAGS = {"music", "radio"}

#: Other spellings the catalog uses for a requested tag.
_TAG_ALIASES = {
    "hip hop": ("hiphop", "hip-hop", "rap"), "hiphop": ("hip hop", "rap"),
    "lofi": ("lo-fi", "lo fi"), "lo-fi": ("lofi", "lo fi"),
    "rnb": ("r&b", "r'n'b", "rhythm and blues"),
    "80s": ("1980s", "eighties"), "90s": ("1990s", "nineties"),
    "70s": ("1970s", "seventies"), "60s": ("1960s", "sixties"),
    "chillout": ("chill", "chill out"), "relax": ("relaxing", "relaxation"),
    "electronic": ("electronica",),
}


def _norm_tag(tag):
    return re.sub(r"[\s\-_'&]+", "", str(tag or "").lower())


def class_tag(cls):
    """The genre tag inside a query class (``"tag=jazz|country=france"``)."""
    for bit in str(cls or "").split("|"):
        if bit.startswith("tag="):
            return bit[4:].strip()
    return ""


def tag_relevance(station, tag):
    """How much *station* is really about *tag*. Purely local, never raises.

    Returns ``{"exact", "name", "share", "foreign", "count", "stuffed"}``.
    Tags are split on commas and compared whole — "rock" is not found inside
    "rockabilly". A tag that contains the genre as a word ("smooth jazz") counts
    as on-genre. ``stuffed`` is the verdict the ranker acts on.
    """
    station = station if isinstance(station, dict) else {}
    want = " ".join(str(tag or "").lower().split())
    wanted = {_norm_tag(want)} | {_norm_tag(a) for a in _TAG_ALIASES.get(want, ())}
    word_re = re.compile(r"(?<!\w)" + re.escape(want) + r"(?!\w)")
    tags = [t.strip().lower() for t in str(station.get("tags") or "").split(",")
            if t.strip()]
    vocab = {_norm_tag(v) for v in RADIO_TAG_WORDS.values()} - wanted
    exact = on_genre = foreign = 0
    for t in tags:
        if _norm_tag(t) in wanted:
            exact += 1
            on_genre += 1
        elif word_re.search(t):
            on_genre += 1
        elif _norm_tag(t) in vocab:
            foreign += 1
    name_hit = bool(word_re.search(str(station.get("name") or "").lower()))
    share = (on_genre / len(tags)) if tags else 0.0
    # Focus is the gate for both shapes: "jazz,blues,soul,funk" is an honest
    # eclectic station (a quarter of it IS jazz), not a stuffed one.
    stuffed = (bool(want) and bool(tags) and not name_hit
               and share < _TAG_FOCUS_SHARE
               and (foreign >= _TAG_FOREIGN_MAX or len(tags) > _TAG_SPAM_COUNT))
    if not want:
        name_hit = False
    return {"exact": bool(exact), "name": name_hit, "share": share,
            "foreign": foreign, "count": len(tags), "stuffed": stuffed}


def rank_stations(stations, limit=8, tag=None):
    """De-duplicate and order a candidate list, most listenable first.

    The old ``_best_station`` returned ONE row (the highest bitrate at or above
    the music bar), which is why every repeat of a query landed on the same
    station. This keeps the same preference — music-grade bitrate beats a
    32 kbps talk relay, which is the lo-fi/france-info failure that rule was
    written for — but hands back the whole shortlist so the caller can vary.

    TuneIn rows publish no bitrate, so a 0 there is "unmeasured", not "bad":
    they sort after the music-grade radio-browser rows and ahead of the
    sub-64k ones rather than at the bottom.

    With a *tag* (a genre request) relevance comes first: see
    :func:`tag_relevance`. Tag-stuffed rows are dropped when enough clean ones
    exist and sorted last otherwise; among the rest, a station that names the
    genre or is focused on it beats one that merely lists it, and bitrate and
    votes only break ties. Without a tag the order is exactly what it was.
    """
    seen, out = set(), []
    for st in (stations or []):
        if not isinstance(st, dict) or not st.get("url_resolved"):
            continue
        key = _station_key(st)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(st)

    def _grade(st):
        bitrate = _as_int(st.get("bitrate"), 0)
        if bitrate >= _STATION_MUSIC_BITRATE:
            return 2
        if bitrate == 0 and str(st.get("source") or "") == "tunein":
            return 1
        return 0

    tag = str(tag or "").strip()
    if tag.lower() in _GENERIC_TAGS:
        tag = ""        # "play some music" names no genre to be relevant to
    rel = {id(st): tag_relevance(st, tag) for st in out} if tag else {}
    if tag:
        clean = [st for st in out if not rel[id(st)]["stuffed"]]
        if len(clean) >= _CLEAN_ENOUGH:
            out = clean

    def _relevance(st):
        r = rel.get(id(st))
        if not r:
            return (0, 0)
        points = 2 * r["name"] + r["exact"] + (r["share"] >= 0.5)
        return (0 if r["stuffed"] else 1, points)

    out.sort(key=lambda s: (-_relevance(s)[0], -_grade(s), -_relevance(s)[1],
                            -_as_int(s.get("votes"), 0),
                            -_as_int(s.get("bitrate"), 0)))
    return out[:max(1, int(limit or 8))]


#: Below this the stream is a talk/news relay or a bad transcode, not music.
#: (Mirrors api_server's constant; kept here so ranking needs no import.)
_STATION_MUSIC_BITRATE = 64


def search_radio(name=None, tag=None, country=None, language=None, limit=20):
    """One structured catalog query across every axis the user actually asked on.

    radio-browser's ``/json/stations/search`` ANDs its parameters, so "french
    jazz" is a single query with both axes rather than a name search that finds
    nothing followed by a tag search that forgets France.

    Falls back down the axes rather than returning nothing: an over-specified
    query ("80s station from Iceland") that matches no row is retried without
    the narrowest axis, so the household gets an honest near-miss instead of
    "no station matched".
    """
    name = (name or "").strip()
    tag = (tag or "").strip()
    language = (language or "").strip()
    axes = {}
    if name:
        axes["name"] = name
    if tag:
        axes["tag"] = tag
    if language:
        axes["language"] = language
    axes.update(_country_params(country))
    if not axes:
        return []

    # A single-axis query is delegated to the axis function it has always used.
    # Not a detour: ``search_stations`` / ``search_stations_by_tag`` are the
    # named choke points the privacy tests record, and a structured search that
    # quietly went around them would make "did this utterance reach the
    # catalog?" unanswerable at exactly the seam that question matters.
    if list(axes) == ["name"]:
        rows = search_stations(name, limit=limit)
        return _with_tunein(rows, name)
    if tag:
        # A genre request is ranked for relevance afterwards, which needs more
        # rows than the shortlist holds (see RADIO_TAG_FETCH_LIMIT).
        limit = max(_as_int(limit, 20), RADIO_TAG_FETCH_LIMIT)
    if list(axes) == ["tag"]:
        return search_stations_by_tag(tag, limit=limit)

    attempts = [axes]
    # Drop the axes back off in order of how much they narrow the result, most
    # first. The NAME axis is never dropped: a named station is the request.
    if len(axes) > 1:
        for drop in ("language", "countrycode", "country", "tag"):
            if drop in attempts[-1] and len(attempts[-1]) > 1:
                looser = {k: v for k, v in attempts[-1].items() if k != drop}
                if "name" in looser or looser:
                    attempts.append(looser)

    rows = []
    for params in attempts:
        rows = _station_search(_station_params(limit, **params))
        if rows:
            break

    return _with_tunein(rows, name)


#: Appended to a "nothing matched" reply when TuneIn was NOT consulted because
#: its own door is shut. One sentence, and it says which of the two things
#: happened: we looked in one place, not both. Never phrase this as TuneIn
#: having been searched — that is the lie the separate door exists to prevent.
TUNEIN_DOOR_SENTENCE = (
    " I only looked on radio-browser — the TuneIn door is closed. It's a "
    "separate switch in Settings > Egress if you want me to look there too.")


def tunein_door_open() -> bool:
    """Is TuneIn's own egress door open? Fails shut, never raises."""
    try:
        from puffpai import tunein_catalog
        return tunein_catalog.door_open()
    except Exception as exc:                              # pragma: no cover
        logger.info("tunein door state unavailable: %s", exc)
        return False


def _with_tunein(rows, name):
    """TuneIn owns the local call letters radio-browser keeps missing.

    Only for a NAME query, and only when radio-browser had little or nothing to
    say: each TuneIn row costs a SECOND request to resolve its stream, so
    merging it into every search would triple the catalog traffic of a turn
    that was already answered.

    TuneIn has its OWN door, so ``radio_browser`` being
    open says nothing about whether this may run. The door is checked here as
    well as inside ``tunein_catalog._get``: with it shut, the merge is a no-op
    and this function neither imports nor calls the catalog.
    """
    rows = list(rows or [])
    if not name or len(rows) >= 2:
        return rows
    if not tunein_door_open():
        return rows
    try:
        from puffpai import tunein_catalog
        return rows + tunein_catalog.search_stations(name, limit=3)
    except Exception as exc:                              # pragma: no cover
        logger.info("tunein lookup failed: %s", exc)
        return rows


def station_stream_url(station):
    """``(url, door_note)`` for one station row — resolving TuneIn if it must.

    A row that already carries ``url_resolved`` (every saved favorite, every
    stored candidate) needs nothing: playing a stream is not a catalog call,
    so a shut TuneIn door never silences a station the household already has.

    A TuneIn row with no URL does need a fresh ``Tune.ashx`` resolve, and with
    the door shut that cannot happen. It returns ``(None, sentence)`` rather
    than a bare ``None``, so the caller can say which switch stopped it
    instead of blaming the station.
    """
    station = station or {}
    url = str(station.get("url_resolved") or "").strip()
    if url:
        return url, None
    uuid = str(station.get("stationuuid") or "")
    is_tunein = (uuid.startswith("tunein:")
                 or str(station.get("source") or "") == "tunein")
    if not is_tunein:
        return None, None
    if not tunein_door_open():
        return None, (
            f"I have {station.get('name') or 'that station'} saved from "
            f"TuneIn, but I'd have to ask TuneIn for its stream again and "
            f"that door is closed — it's a separate switch in "
            f"Settings > Egress.")
    try:
        from puffpai import tunein_catalog
        return tunein_catalog.resolve_stream(uuid.split(":", 1)[-1]), None
    except Exception as exc:                              # pragma: no cover
        logger.info("tunein resolve failed: %s", exc)
        return None, None


def search_for_text(text, limit=20):
    """Interpret free text and run the matching structured search.

    Returns ``(stations, plan)`` so the caller can say what it searched for.
    """
    plan = interpret_radio_query(text)
    if not plan:
        return [], plan
    return search_radio(limit=limit, **plan), plan


# ══════════════════════════════════════════════════════════════════════════════
#  PLAY HISTORY, CANDIDATE LIST AND FAVORITES  (mediahub app_data)
# ══════════════════════════════════════════════════════════════════════════════
#
# All three live in the mediahub doc beside the player slots, written through
# ``AppData.mediahub_set`` — which refuses to touch ``radio`` / ``radio_display``,
# so the appliance-vs-display ownership rules above cannot be broken from here.

#: How many recent plays are remembered per query class before the variety rule
#: stops avoiding them. Five: long enough that "a jazz station" feels different
#: every time for a week, short enough that a household with six reachable jazz
#: stations never runs out of candidates.
RADIO_HISTORY_AVOID = 5
RADIO_HISTORY_MAX = 40
RADIO_FAVORITES_MAX = 40


def _doc(app_data):
    try:
        doc = (app_data or _get_app_data()).get_data("mediahub") or {}
    except Exception:
        return {}
    return doc if isinstance(doc, dict) else {}


def query_class(plan):
    """A stable label for "the same kind of ask", used by the variety rule.

    A NAME query is its own class and is never varied — "play KEXP" must play
    KEXP every time. A genre/country/vague ask shares a class with every other
    phrasing of it, so "some jazz" and "a jazz station" avoid each other's
    recent plays.
    """
    plan = plan or {}
    if plan.get("name"):
        return "name:" + " ".join(str(plan["name"]).lower().split())
    bits = [f"{k}={plan[k]}" for k in ("tag", "country", "language")
            if plan.get(k)]
    return "|".join(bits).lower() if bits else "any"


def list_favorites(app_data=None):
    favs = _doc(app_data).get("radio_favorites")
    return [f for f in favs if isinstance(f, dict)] if isinstance(favs, list) else []


# ── one stored shape, two surfaces ───────────────────────────────────────────
#
# The dashboard's World Radio player does not keep the household's hearts in
# browser localStorage, which would make two lists, neither visible to the
# other. The four ``mediahub_favorites_*`` app actions
# below make THIS the list, so a station saved by voice shows up in the browser
# and a heart tapped in the browser plays when she says "play my favorites".
#
# That makes every field here UNTRUSTED BROWSER INPUT that a later "play my
# favorites" hands to mpv. So nothing is stored that has not been through
# :func:`sanitize_favorite`: unknown keys dropped, strings de-controlled and
# capped, and the stream URL put through the player's own refusal check in its
# no-DNS mode (the full check, lookups and all, still runs at play time).

#: How many rows one import call will even look at. The browser list is capped
#: at RADIO_FAVORITES_MAX anyway; this bounds the work a single request can ask
#: for when the body is not what the dashboard would have sent.
FAVORITES_IMPORT_CONSIDERED = 100

#: Per-row size bound before any field is read, so a megabyte of junk in one
#: key is refused rather than trimmed forty times over.
_FAVORITE_ROW_BYTES = 16384

#: Length caps per stored field. ``name`` is NOT here: an over-long name makes
#: the row invalid rather than truncated, because a name is the thing she says.
_FAVORITE_CAPS = {"tags": 300, "country": 80, "countrycode": 8,
                  "codec": 20, "uuid": 200}
_FAVORITE_NAME_MAX = 120
_FAVORITE_LINK_MAX = 500
_FAVORITE_URL_MAX = 2000


#: The TuneIn guide id, the ONE form a stored TuneIn identity may take.
#: Mirrors tunein_catalog._GUIDE_ID_RE, because the id is split back out of
#: this string and put in a catalog request.
_TUNEIN_UUID_RE = re.compile(r"^tunein:s\d+$")

#: Our own synthetic wire id for a favorite with no catalog uuid. Recognised on
#: the way back IN so a row the page round-trips is not mistaken for a station
#: whose catalog uuid happens to be one of ours.
_SYNTHETIC_ID_RE = re.compile(r"^url:[0-9a-f]{16}$")


def _strip_controls(text):
    """C0, DEL, C1 and Unicode format characters (Cf) removed.

    Cf is in here because U+202E and friends reorder what a human reads
    without changing what a machine stores — a station called "Jazz‮..."
    can be made to display as something else entirely on her wall.
    """
    import unicodedata
    return "".join(ch for ch in text
                   if not (ch < " " or "\x7f" <= ch <= "\x9f"
                           or unicodedata.category(ch) == "Cf"))


def _clean_text(value, limit):
    """Text with controls removed, whitespace collapsed, capped.

    A non-string is "" — never ``str(value)``, which would store the repr of
    a dict or list the browser sent where a name or a genre should be.
    """
    if not isinstance(value, str):
        return ""
    return " ".join(_strip_controls(value).split())[:limit]


def _web_link(value):
    """A link safe to show, or "" — the same check the stream URL gets.

    homepage/favicon are rendered by the page (a favicon as an ``<img src>``),
    so they are the same class of thing as the stream URL: an address chosen
    by a third-party catalog that this house then fetches. They get the same
    syntactic refusal — http(s) only, no credentials, nothing indoor — and are
    DROPPED rather than making the whole row invalid, because a bad favicon is
    not a reason to lose a station.
    """
    if not isinstance(value, str):
        return ""
    link = _strip_controls(value).strip()
    if not link or len(link) > _FAVORITE_LINK_MAX:
        return ""  # truncating a URL invents a different one; drop it instead
    try:
        from puffpai import radio_player
        if radio_player.stream_url_refusal(link, resolve_host=False) is not None:
            return ""
    except Exception:
        return ""
    return link


def _fav_bitrate(value):
    try:
        rate = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return rate if 0 <= rate <= 10000 else 0


def sanitize_favorite(station):
    """One untrusted station row -> the stored favorite shape, or None.

    None means "do not store this": no name, no playable stream URL, or a URL
    the player would refuse anyway (file://, ``--script=``, 127.0.0.1, a
    ``.local`` name, control characters). Everything else is kept but bounded.
    """
    if not isinstance(station, dict):
        return None
    try:
        if len(json.dumps(station, default=str)) > _FAVORITE_ROW_BYTES:
            return None
    except Exception:
        return None

    if not isinstance(station.get("name"), str):
        return None
    name = _clean_text(station.get("name"), _FAVORITE_NAME_MAX + 1)
    if not name or len(name) > _FAVORITE_NAME_MAX:
        return None

    url = station.get("url_resolved") or station.get("url") or ""
    if not isinstance(url, str) or len(url) > _FAVORITE_URL_MAX:
        return None
    url = url.strip()
    # A stream URL with a control or bidi character in it is refused outright
    # rather than cleaned: the cleaned string would be a DIFFERENT address.
    if _strip_controls(url) != url:
        return None
    try:
        from puffpai import radio_player
        # Storage-time mode: same refusals, no DNS. See stream_url_refusal.
        if radio_player.stream_url_refusal(url, resolve_host=False) is not None:
            return None
    except Exception as exc:  # never let validation failure become a store
        logger.warning("favorite url check unavailable: %s", exc)
        return None

    uuid = _clean_text(station.get("stationuuid") or station.get("uuid"),
                       _FAVORITE_CAPS["uuid"])
    if _SYNTHETIC_ID_RE.match(uuid.lower()):
        uuid = ""          # ours, not a catalog's: the URL is the identity
    source = _clean_text(station.get("source"), 20).lower()
    # ONE TuneIn convention: the identity is the stationuuid spelled
    # ``tunein:s<digits>``, lowercase, and ``source`` mirrors it.
    # station_stream_url accepts either signal and splits the guide id back
    # out of the uuid to re-resolve the stream, so a "tunein:" uuid that is
    # not a guide id is not a station we could ever play — refuse the row.
    if uuid.lower().startswith("tunein:") or source == "tunein":
        source = "tunein"
        if uuid and not uuid.lower().startswith("tunein:"):
            uuid = "tunein:" + uuid
        uuid = uuid.lower()
        if not _TUNEIN_UUID_RE.match(uuid):
            return None
    else:
        source = "radio-browser"

    return {
        "name": name,
        "url": url,
        "uuid": uuid,
        "source": source,
        "tags": _clean_text(station.get("tags"), _FAVORITE_CAPS["tags"]),
        "country": _clean_text(station.get("country"), _FAVORITE_CAPS["country"]),
        "countrycode": _clean_text(station.get("countrycode"),
                                   _FAVORITE_CAPS["countrycode"]),
        "codec": _clean_text(station.get("codec"), _FAVORITE_CAPS["codec"]),
        "bitrate": _fav_bitrate(station.get("bitrate")),
        "homepage": _web_link(station.get("homepage")),
        "favicon": _web_link(station.get("favicon")),
    }


def _fav_row(station):
    """Back-compat name for :func:`sanitize_favorite`."""
    return sanitize_favorite(station)


def _norm_url(url):
    """A URL comparable to another URL: scheme and host case-folded."""
    from urllib.parse import urlsplit, urlunsplit
    raw = str(url or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    netloc = (parts.netloc or "").lower()
    path = (parts.path or "").rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path,
                       parts.query, "")).lower()


def favorite_identity(row):
    """What makes two favorites the same station: the uuid, else the URL."""
    row = row if isinstance(row, dict) else {}
    uuid = str(row.get("uuid") or row.get("stationuuid") or "").strip()
    if uuid:
        return "id:" + uuid.lower()
    return "url:" + _norm_url(row.get("url") or row.get("url_resolved") or "")


def _same_favorite(existing, row):
    """Same station? Identity only — NOT the name.

    Two different stations sharing a name is routine in radio-browser (four
    "Radio Nova"s, four countries), so starring the second must not rewrite
    the first. The name still resolves the VOICE remover ("remove Radio Nova
    from my favorites"), which takes the first match and says which — see
    :func:`remove_favorite`.
    """
    return favorite_identity(existing) == favorite_identity(row)


def _synthetic_id(url):
    """A stable wire id for a favorite the catalog gave no uuid.

    ``stationuuid: ""`` is falsy in the browser and the row gets dropped by
    anything keying on it, so uuid-less favorites need SOME id. A hash of the
    normalised URL is stable across reloads, carries no query string into the
    DOM, and is recognised on the way back in (see sanitize_favorite) so it is
    never mistaken for a catalog uuid.
    """
    import hashlib
    digest = hashlib.sha1(_norm_url(url).encode("utf-8", "replace")).hexdigest()
    return "url:" + digest[:16]


def favorite_wire(fav):
    """A stored favorite in the shape the dashboard's ``Station`` type wants."""
    fav = fav if isinstance(fav, dict) else {}
    return {
        "stationuuid": str(fav.get("uuid") or "") or _synthetic_id(fav.get("url")),
        "name": str(fav.get("name") or ""),
        "url_resolved": str(fav.get("url") or ""),
        "country": str(fav.get("country") or ""),
        "countrycode": str(fav.get("countrycode") or ""),
        "codec": str(fav.get("codec") or ""),
        "bitrate": _as_int(fav.get("bitrate"), 0),
        "tags": str(fav.get("tags") or ""),
        "homepage": str(fav.get("homepage") or ""),
        "favicon": str(fav.get("favicon") or ""),
        "source": str(fav.get("source") or "radio-browser"),
    }


def _fav_station(fav):
    """A favorite row back in the shape the player and ranking expect."""
    station = favorite_wire(fav)
    station["votes"] = 0
    return station


def _save_favorites(ad, favs):
    """Write the list through the closed-key writer. False if it did not."""
    try:
        ad.mediahub_set("radio_favorites", favs[:RADIO_FAVORITES_MAX])
        return True
    except Exception as exc:
        # Names and counts only: a station URL can carry a query string.
        logger.warning("could not write radio favorites (%d rows): %s",
                       len(favs), exc)
        return False


def _put_favorite(row, ad):
    """Insert or update one sanitized row. Returns an action result dict."""
    favs = list_favorites(ad)
    for index, existing in enumerate(favs):
        if _same_favorite(existing, row):
            favs[index] = row          # update in place, position kept
            break
    else:
        if len(favs) >= RADIO_FAVORITES_MAX:
            return {"ok": False, "error": "favorites_full",
                    "max": RADIO_FAVORITES_MAX}
        favs.insert(0, row)            # newest first, as voice saves have been
    if not _save_favorites(ad, favs):
        return {"ok": False, "error": "save_failed"}
    return {"ok": True, "favorites": [favorite_wire(f) for f in favs]}


def add_favorite(station, app_data=None):
    """Save *station* as a favorite. Returns (ok, name).

    Idempotent: the same station (by uuid, else URL, else name) updates in
    place instead of piling up. ``(False, None)`` means the row was refused as
    unplayable/invalid; ``(False, name)`` means it was valid but not written
    (list full, or the write failed).
    """
    ad = app_data or _get_app_data()
    row = sanitize_favorite(station or {})
    if row is None:
        return False, None
    result = _put_favorite(row, ad)
    return bool(result.get("ok")), row["name"]


def remove_favorite(name, app_data=None):
    """Drop a favorite by (fuzzy) name. Returns the removed name, or None."""
    ad = app_data or _get_app_data()
    want = " ".join(str(name or "").lower().split())
    if not want:
        return None
    favs = list_favorites(ad)
    match = next((f for f in favs
                  if str(f.get("name", "")).lower() == want), None)
    if match is None:
        match = next((f for f in favs
                      if want in str(f.get("name", "")).lower()), None)
    if match is None:
        return None
    try:
        ad.mediahub_set("radio_favorites",
                        [f for f in favs if f is not match])
    except Exception as exc:
        logger.warning("could not remove radio favorite: %s", exc)
        return None
    return match.get("name")


# ── the four dashboard actions (POST /apps/mediahub/action) ──────────────────
#
# Each returns a plain dict and NEVER raises: the route turns an exception into
# a 500 with the message in it, which is neither useful to the page nor
# something to hand a browser. A refusal is {"ok": false, "error": ...} with
# nothing written.

def favorites_view(app_data=None):
    """{"ok": true, "favorites": [...], "max": n} — the whole list."""
    try:
        return {"ok": True,
                "favorites": [favorite_wire(f) for f in list_favorites(app_data)],
                "max": RADIO_FAVORITES_MAX}
    except Exception as exc:
        logger.warning("could not read radio favorites: %s", exc)
        return {"ok": False, "error": "unavailable"}


def favorites_add(station, app_data=None):
    """Add or update one station from the dashboard. Untrusted input."""
    try:
        row = sanitize_favorite(station)
        if row is None:
            logger.info("radio favorite refused (invalid station row)")
            return {"ok": False, "error": "invalid_station"}
        return _put_favorite(row, app_data or _get_app_data())
    except Exception as exc:
        logger.warning("could not add radio favorite: %s", exc)
        return {"ok": False, "error": "unavailable"}


def favorites_remove(station_id=None, url=None, app_data=None):
    """Drop one station by stationuuid or URL. Removing an absent row is ok."""
    try:
        want_id = _clean_text(station_id, _FAVORITE_CAPS["uuid"]).lower()
        want_url = _norm_url(url) if isinstance(url, str) and url.strip() else ""
        if not want_id and not want_url:
            return {"ok": False, "error": "invalid_id"}
        # "url:<16 hex>" is the id WE gave a uuid-less row on the wire, so it
        # has to resolve back to that row's URL and nothing else.
        want_hash = want_id if _SYNTHETIC_ID_RE.match(want_id) else ""
        ad = app_data or _get_app_data()
        favs = list_favorites(ad)

        def _hit(fav):
            uuid = str(fav.get("uuid") or "").strip().lower()
            if want_id and uuid and uuid == want_id:
                return True
            if want_hash and not uuid and _synthetic_id(fav.get("url")) == want_hash:
                return True
            return bool(want_url) and _norm_url(fav.get("url")) == want_url

        kept = [f for f in favs if not _hit(f)]
        if len(kept) != len(favs) and not _save_favorites(ad, kept):
            return {"ok": False, "error": "save_failed"}
        return {"ok": True, "favorites": [favorite_wire(f) for f in kept]}
    except Exception as exc:
        logger.warning("could not remove radio favorite: %s", exc)
        return {"ok": False, "error": "unavailable"}


def favorites_import(stations, app_data=None):
    """Lift one browser's localStorage hearts into the household list.

    Merge, never replace: rows already present are skipped, existing order is
    kept, and the import stops at the cap. Invalid rows are skipped rather
    than failing the whole import — one bad row out of forty must not cost her
    the other thirty-nine.

    Three counts, because they mean different things to the page:
    ``added``, ``skipped`` (looked at and not taken: invalid, duplicate, or no
    room) and ``ignored`` (never looked at — rows past the consideration
    limit). ``full`` says the cap is why, so the page can say so.
    """
    try:
        if not isinstance(stations, list):
            return {"ok": False, "error": "invalid_stations"}
        ad = app_data or _get_app_data()
        favs = list_favorites(ad)
        considered = stations[:FAVORITES_IMPORT_CONSIDERED]
        ignored = len(stations) - len(considered)
        added = 0
        full = False
        for candidate in considered:
            if len(favs) >= RADIO_FAVORITES_MAX:
                full = True            # the rest are skipped for room, not merit
                break
            row = sanitize_favorite(candidate)
            if row is None:
                continue
            if any(_same_favorite(existing, row) for existing in favs):
                continue
            favs.append(row)           # append: the browser's order survives
            added += 1
        if added and not _save_favorites(ad, favs):
            return {"ok": False, "error": "save_failed"}
        skipped = len(considered) - added
        logger.info("radio favorites import: %d added, %d skipped, %d ignored",
                    added, skipped, ignored)
        return {"ok": True,
                "favorites": [favorite_wire(f) for f in favs],
                "added": added,
                "skipped": skipped,
                "ignored": ignored,
                "full": full}
    except Exception as exc:
        logger.warning("could not import radio favorites: %s", exc)
        return {"ok": False, "error": "unavailable"}


def recent_names(cls, app_data=None):
    """Station names recently played for query class *cls*, newest first."""
    hist = _doc(app_data).get("radio_history")
    if not isinstance(hist, list):
        return []
    return [str(h.get("name") or "") for h in hist
            if isinstance(h, dict) and h.get("class") == cls]


def note_played(cls, station, app_data=None):
    """Remember that *station* was played for query class *cls*."""
    ad = app_data or _get_app_data()
    name = str((station or {}).get("name") or "").strip()
    if not name:
        return
    hist = _doc(ad).get("radio_history")
    hist = [h for h in hist if isinstance(h, dict)] if isinstance(hist, list) else []
    hist.insert(0, {"class": cls, "name": name, "at": _now_iso()})
    try:
        ad.mediahub_set("radio_history", hist[:RADIO_HISTORY_MAX])
    except Exception as exc:
        logger.warning("could not record radio history: %s", exc)


def store_candidates(cls, stations, app_data=None):
    """Persist the shortlist so "play the second one" / "something else" work."""
    ad = app_data or _get_app_data()
    rows = [s for s in (stations or []) if isinstance(s, dict)][:8]
    try:
        ad.mediahub_set("radio_candidates",
                        {"class": cls, "at": _now_iso(), "stations": rows})
    except Exception as exc:
        logger.warning("could not store radio candidates: %s", exc)


def stored_candidates(app_data=None):
    row = _doc(app_data).get("radio_candidates")
    if not isinstance(row, dict):
        return "", []
    rows = row.get("stations")
    return str(row.get("class") or ""), [s for s in rows
                                         if isinstance(s, dict)] if isinstance(rows, list) else []


def pick_varied(cls, stations, app_data=None):
    """The best candidate that ISN'T one of the last few played for this class.

    A NAME class is returned unvaried: a station asked for by name is the
    request, not a suggestion. Everything else rotates, which is the whole
    answer to "she keeps defaulting back to a few she likes".
    """
    ranked = rank_stations(stations, limit=8, tag=class_tag(cls))
    if not ranked:
        return None
    if str(cls or "").startswith("name:"):
        return ranked[0]
    avoid = {n.lower() for n in recent_names(cls, app_data)[:RADIO_HISTORY_AVOID]}
    for st in ranked:
        if str(st.get("name") or "").lower() not in avoid:
            return st
    return ranked[0]


# ══════════════════════════════════════════════════════════════════════════════
#  SPOTIFY EVENT NORMALISATION
# ══════════════════════════════════════════════════════════════════════════════

# librespot's player_event_handler exports every field as an env var and the
# ONEVENT hook shape has changed between releases (PLAYER_EVENT vs event,
# ARTISTS vs artists, sometimes a flat string, sometimes a list). Nothing here
# may raise: a 500 on this endpoint would make librespot retry a bad payload
# forever, and an unrecognised event is not an error condition.
_EVENT_KEYS = ("player_event", "event", "event_name", "type")
_TITLE_KEYS = ("name", "track_name", "title", "item_name")
_ARTIST_KEYS = ("artists", "artist", "album_artists", "show_name")
_ALBUM_KEYS = ("album", "album_name")
_URI_KEYS = ("uri", "track_id", "item_id", "track_uri")
_USER_KEYS = ("user_name", "username", "user")

# Events that mean nobody is casting to us any more.
_INACTIVE_EVENTS = {"session_disconnected", "stopped", "sink_inactive", "shutdown"}
# Events that carry no track/session meaning — they must not blank the state.
_NEUTRAL_EVENTS = {"volume_changed", "volume_set", "preload_next", "position_correction"}
# Sound is coming out / has stopped coming out. Anything unlisted inherits the
# previous flag: "active" (session exists) and "playing" (audible now) are
# separate axes — a paused session is active but must not show a live dot.
_PLAYING_EVENTS = {"playing", "track_changed", "started"}
_NOT_PLAYING_EVENTS = {"paused", "loading", "preloading"}


def _lower_keys(payload):
    """Flatten to a lowercase-keyed dict. Tolerates non-dict input."""
    if not isinstance(payload, dict):
        return {}
    return {str(k).strip().lower(): v for k, v in payload.items()}


def _first(flat, keys):
    for k in keys:
        v = flat.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _as_artist_string(val):
    """librespot sends artists as a list, a newline/semicolon-joined string, or
    a single name depending on version."""
    if val is None:
        return None
    if isinstance(val, (list, tuple)):
        return ", ".join(str(a).strip() for a in val if str(a).strip()) or None
    text = str(val).strip()
    if not text:
        return None
    parts = [p.strip() for p in re.split(r"[\n;\|]+", text) if p.strip()]
    return ", ".join(parts) if parts else None


def normalize_spotify_event(payload, previous=None):
    """Fold a raw librespot event into the stored spotify state.

    *previous* is the last stored state; track metadata only arrives on
    track_changed, so a bare "playing" event must not blank what we know.
    Returns {active, user, track, last_event, updated_at, debug}.
    """
    flat = _lower_keys(payload)
    prev = previous if isinstance(previous, dict) else {}

    event = str(_first(flat, _EVENT_KEYS) or "unknown").strip().lower()

    title = _first(flat, _TITLE_KEYS)
    artists = _as_artist_string(_first(flat, _ARTIST_KEYS))
    album = _first(flat, _ALBUM_KEYS)
    uri = _first(flat, _URI_KEYS)

    track = None
    if title or artists or uri:
        track = {
            "title": str(title).strip() if title else None,
            "artists": artists,
            "album": str(album).strip() if album else None,
            "uri": str(uri).strip() if uri else None,
        }
    elif event in _NEUTRAL_EVENTS or event not in _INACTIVE_EVENTS:
        track = prev.get("track")  # carry forward; this event had no metadata

    user = _first(flat, _USER_KEYS)
    user = str(user).strip() if user else prev.get("user")

    if event in _INACTIVE_EVENTS:
        active = False
        if event == "session_disconnected":
            track, user = None, None
    elif event in _NEUTRAL_EVENTS:
        active = bool(prev.get("active"))
    else:
        active = True

    if event in _INACTIVE_EVENTS or event in _NOT_PLAYING_EVENTS:
        playing = False
    elif event in _PLAYING_EVENTS:
        playing = True
    else:
        playing = bool(prev.get("playing"))

    state = {
        "active": active,
        "playing": playing,
        "user": user,
        "track": track,
        "last_event": event,
        "updated_at": _now_iso(),
    }

    try:
        raw = json.dumps(payload, default=str)[:MAX_DEBUG_BYTES]
    except Exception:
        # A payload nested past the interpreter's recursion limit defeats
        # json.dumps AND repr (RecursionError on Python <= 3.11), so the
        # fallback needs its own guard — the debug capture must never raise.
        try:
            raw = str(payload)[:MAX_DEBUG_BYTES]
        except Exception:
            raw = "<unserialisable %s payload>" % type(payload).__name__
    state["debug"] = {"last_raw": raw}
    return state


# ══════════════════════════════════════════════════════════════════════════════
#  AGGREGATION
# ══════════════════════════════════════════════════════════════════════════════

def _gated_radio_row(raw, doc, allow_probe):
    """One radio slot, freshness-gated, in the shape the tile expects.

    Always the same shape, on every path including a missing or corrupt doc: a
    caller keying off row["status"] must never hit a KeyError just because the
    degraded branch answered.
    """
    row = {"status": "idle", "station": None}
    if isinstance(raw, dict):
        row.update(raw)
    # A player that stopped checking in is not playing anything, whatever it
    # last reported — paused included, or a dead kiosk leaves a phantom
    # "paused" row forever. Freshest of the state write and the poll heartbeat
    # decides.
    if row.get("status") in ("playing", "connecting", "paused"):
        # BOTH signals must be fresh. The heartbeat alone only proves the
        # display is alive, not that this state is current (a reloaded kiosk
        # polls immediately but starts idle); the state write alone goes
        # silent when the kiosk dies. The bridge re-reports every 20s while a
        # station is set, so genuine playback keeps both fresh.
        age_state = _age_seconds(row.get("updated_at"))
        age_seen = _age_seconds(_radio_last_seen(doc))
        row["stale"] = (
            age_state is None or age_state > RADIO_STALE_SECONDS
            or age_seen is None or age_seen > RADIO_STALE_SECONDS
        )
        # ...unless the appliance itself is the player, in which case we can
        # verify the stream directly instead of inferring it from a heartbeat
        # nobody is sending. The container says nothing about what a browser
        # is doing, so the display slot never asks.
        if row["stale"] and allow_probe and row.get("source") == "appliance":
            if _appliance_radio_live():
                row["stale"] = False
    else:
        row["stale"] = False
    return row


def aggregate_sources(app_data, jellyfin_sessions=None):
    """Per-source snapshot for the Now Playing tile.

    Never raises: this rides along on /api/media/now-playing and must not be
    able to take the Jellyfin payload down with it.
    """
    try:
        try:
            doc = app_data.get_data("mediahub") or {}
        except Exception:
            doc = {}
        if not isinstance(doc, dict):
            doc = {}

        raw_radio = doc.get("radio")
        raw_display = doc.get("radio_display")
        # MIGRATION. Before the slots were split, a display
        # report overwrote the appliance's row, so a file written by the old
        # code can carry a display-owned row — usually an "idle" one — in the
        # appliance slot. Read it as what it is instead of letting it
        # masquerade as the appliance. A row with NO source is legacy
        # appliance-or-unknown and stays where it is.
        if (isinstance(raw_radio, dict)
                and str(raw_radio.get("source") or "").strip().lower() == "display"):
            if not isinstance(raw_display, dict):
                raw_display = raw_radio
            raw_radio = None
        # Always the same shape, on every path including a missing or corrupt
        # doc: a caller keying off radio["status"] must never hit a KeyError
        # just because the degraded branch answered.
        radio = _gated_radio_row(raw_radio, doc, allow_probe=True)
        # Additive key: the kiosk bridge's own view of its browser player. The
        # tile reads its "this device" row straight from the browser audio
        # manager and ignores unknown keys, so nothing on the front end needs
        # to change for this to exist.
        radio_display = _gated_radio_row(raw_display, doc, allow_probe=False)

        raw_spotify = doc.get("spotify")
        spotify = {"active": False, "playing": False, "track": None}
        if isinstance(raw_spotify, dict):
            spotify.update(raw_spotify)
        spotify.pop("debug", None)  # the tile has no use for the raw payload
        # No heartbeat exists for Spotify (see SPOTIFY_STALE_SECONDS): an event
        # row from weeks ago kept the tile showing a track nobody was playing.
        # The user label is kept — it is who the speaker is paired to, not a
        # claim about playback.
        spotify_age = _age_seconds(spotify.get("updated_at"))
        if spotify_age is not None and spotify_age > SPOTIFY_STALE_SECONDS:
            spotify["active"] = False
            spotify["playing"] = False
            spotify["track"] = None

        sessions = jellyfin_sessions if isinstance(jellyfin_sessions, list) else []

        return {
            "radio": radio,
            "radio_display": radio_display,
            "spotify": spotify,
            "jellyfin": {"session_count": len(sessions)},
        }
    except Exception:
        # "Never raises" is a load-bearing promise: this rides on
        # /api/media/now-playing and a corrupt mediahub.json must not take the
        # Jellyfin payload down with it.
        return {
            "radio": {"status": "idle", "station": None, "stale": True},
            "radio_display": {"status": "idle", "station": None, "stale": True},
            "spotify": {"active": False, "playing": False, "track": None},
            "jellyfin": {"session_count": 0},
        }


def radio_display_online(app_data):
    """True when the dashboard radio player has polled us recently."""
    try:
        doc = app_data.get_data("mediahub") or {}
    except Exception:
        doc = {}
    age = _age_seconds(_radio_last_seen(doc))
    return age is not None and age <= RADIO_STALE_SECONDS


# ══════════════════════════════════════════════════════════════════════════════
#  LLM TOOL BODIES  (assistant_tools wraps these one-for-one)
# ══════════════════════════════════════════════════════════════════════════════

def _get_app_data():
    from puffpai.app_data import AppData
    return AppData()


def _station_label(station):
    bits = [station.get("name") or "a station"]
    detail = []
    if station.get("country"):
        detail.append(station["country"])
    codec = (station.get("codec") or "").upper()
    bitrate = station.get("bitrate") or 0
    if codec and bitrate:
        detail.append(f"{codec} {bitrate}k")
    elif codec:
        detail.append(codec)
    if detail:
        bits.append(f"({', '.join(detail)})")
    return " ".join(bits)


def tool_radio_play(query, app_data=None):
    """Search the catalog, queue the top station for the dashboard player."""
    ad = app_data or _get_app_data()
    query = (query or "").strip().strip("'\"")
    if "=" in query and " " not in query.split("=")[0]:
        query = query.split("=")[-1].strip("'\"")
    if not query:
        return "Tell me what to tune in — a station name, genre, or city."

    # Say which door is shut instead of reporting "no station matched", which
    # would be a true-sounding sentence about the wrong thing.
    from puffpai.security.egress_doors import EgressBlocked, require_open
    try:
        require_open("radio_browser")
    except EgressBlocked as blocked:
        return blocked.user_message

    stations, plan = search_for_text(query, limit=12)
    if not stations:
        stations = search_stations(query, limit=10)
        plan = {"name": query}
    if not stations:
        msg = (f"No internet radio station matched '{query}'. "
               "Try a genre (jazz, news, salsa), a country, or a station name.")
        # Say WHICH catalogs were asked. TuneIn has its
        # own door, so "nothing matched" with that door shut means one catalog
        # was consulted, not two — and the household is owed that difference.
        if not tunein_door_open():
            msg += TUNEIN_DOOR_SENTENCE
        return msg

    cls = query_class(plan)
    ranked = rank_stations(stations, limit=8, tag=(plan or {}).get("tag"))
    store_candidates(cls, ranked, ad)
    station = pick_varied(cls, ranked, ad) or ranked[0]
    note_played(cls, station, ad)
    try:
        ad.mediahub_radio_request("play", station=station)
    except Exception as exc:
        return f"Could not queue the station: {exc}"

    # HONEST WORDING. This is the BROWSER player, reached only
    # when the appliance player could not start the stream. Saying "on the
    # speakers" here was the difference between a reply that matched the room
    # and one that did not: with no dashboard open, this makes no sound at all.
    msg = (f"I queued {_station_label(station)} on the dashboard player — it "
           f"plays on whatever screen has PuffPAI open, not the appliance "
           f"speakers.")
    if not radio_display_online(ad):
        msg += (" The dashboard radio player hasn't checked in recently, so "
                "nothing will be audible until a screen wakes up.")
    return msg


#: Genres sampled when someone asks what radio is available. Four, not ten:
#: each is one catalog round trip (~6 s worst case per mirror) and the answer
#: only has to prove the capability is real, not enumerate it.
RADIO_DISCOVERY_TAGS = ("news", "jazz", "classical", "country")


def tool_radio_stations(query: str = "", app_data=None):
    """What radio this household can ACTUALLY tune, from the live catalog.

    Without it, "what radio stations can i listen to" makes zero tool calls,
    goes ``Route: rag``, and is answered out of the offline encyclopedia:
    local FM/AM, NPR "if you have a satellite dish", "amateur repeater feeds on
    the ham band", military listening posts, while World Radio and TuneIn are
    wired and working and "play some jazz radio" starts a real station. With
    no reader for the discovery phrasing, the model fills the gap with
    plausible prose about equipment the household does not own.

    Every station named here comes back from a radio-browser query made in
    this call. If the egress door is shut or no mirror answers, this says so
    instead of naming a station.
    """
    from puffpai.security.egress_doors import EgressBlocked, require_open
    try:
        require_open("radio_browser")
    except EgressBlocked as blocked:
        return blocked.user_message

    asked = (query or "").strip().strip("'\"")
    if "=" in asked and " " not in asked.split("=")[0]:
        asked = asked.split("=")[-1].strip("'\"")

    favs = list_favorites(app_data)

    # FIND WITHOUT PLAYING. The old body sampled four fixed
    # genres one station at a time, so "what jazz stations are there" answered
    # with one jazz station and three it had not been asked about, and nothing
    # it named could then be asked for by position. It now runs the SAME
    # structured search the player runs and hands back the shortlist it would
    # have played from — which is also what makes "play the second one" work.
    plan = interpret_radio_query(asked) if asked else {}
    stations = []
    if plan:
        try:
            stations = search_radio(limit=12, **plan)
        except Exception as exc:
            logger.info("radio discovery search failed: %s", exc)
    if not stations and not plan:
        for tag in RADIO_DISCOVERY_TAGS:
            try:
                stations.extend(search_stations_by_tag(tag, limit=2))
            except Exception:
                continue
        plan = {}

    ranked = rank_stations(stations, limit=6, tag=(plan or {}).get("tag"))
    if ranked:
        cls = query_class(plan)
        try:
            store_candidates(cls, ranked, app_data)
        except Exception:
            pass
        media_log(f"radio stations query={asked!r} -> {len(ranked)} candidates")
        header = (f"Stations I can reach for '{asked}' right now:" if asked
                  else "A few stations I can reach right now:")
        body = "\n".join(f"{i}. {_station_label(s)}"
                         for i, s in enumerate(ranked, 1))
        tail = ("\n\nSay \"play <name>\" — or \"play the second one\" — and "
                "I'll tune it on the appliance speakers.")
        if favs:
            tail += ("\nYour saved favorites: "
                     + ", ".join(str(f.get("name")) for f in favs) + ".")
        return f"{header}\n{body}{tail}"

    if favs:
        return ("I couldn't reach the internet radio catalog just now. Your "
                "saved favorite stations still play: "
                + ", ".join(str(f.get("name")) for f in favs) + ".")
    if asked:
        return (f"I searched the internet radio catalog for '{asked}' just "
                f"now and nothing came back, so I won't guess at a station. "
                f"Try a different genre, a country, or a station name.")
    return ("I couldn't reach the internet radio catalog just now, so I "
            "can't tell you which stations are available without guessing "
            "— and I won't. Try again in a moment, or ask me to play a "
            "station by name and I'll search for it then.")


def tool_radio_favorites(action: str = "list", name: str = "", app_data=None):
    """List / save / remove favorite radio stations (the BACKEND list).

    The dashboard's hearts live in browser localStorage and are invisible to
    voice; this is the household's list, readable from any surface. Saving
    takes the station the APPLIANCE is playing — the only one we can prove.
    """
    ad = app_data or _get_app_data()
    action = (action or "list").strip().lower()

    if action in ("save", "add", "favorite", "favourite"):
        doc = _doc(ad)
        row = doc.get("radio")
        station = (row or {}).get("station") if isinstance(row, dict) else None
        if not station:
            return ("Nothing is playing on the radio right now, so there's no "
                    "station for me to save.")
        ok, saved = add_favorite(station, ad)
        if ok:
            return f"Saved {saved} to your favorite stations."
        if saved and len(list_favorites(ad)) >= RADIO_FAVORITES_MAX:
            return (f"Your favorites list is full at {RADIO_FAVORITES_MAX} "
                    f"stations, so I didn't save {saved}. Tell me one to "
                    f"remove and I'll put it in.")
        return "I couldn't save that station — it has no playable stream on file."

    if action in ("remove", "delete", "forget"):
        gone = remove_favorite(name, ad)
        return (f"Removed {gone} from your favorite stations." if gone else
                f"I don't have a favorite station matching '{name}'.")

    favs = list_favorites(ad)
    if not favs:
        return ("You haven't saved any favorite radio stations yet. While one "
                "is playing, say \"save this station\".")
    return ("Your favorite stations: "
            + ", ".join(str(f.get("name")) for f in favs) + ".")


def radio_truth_check(app_data=None) -> None:
    """Log (WARNING) when the two things we can observe about appliance radio
    disagree. Never raises, never speaks - it is a breadcrumb for the
    "what's playing" beat, where the honest answer depends on these agreeing.

    Two disagreements are real, and only two:

    * a running ``puffpai-radio`` container with ZERO Pulse sink-inputs - mpv
      is alive but making no sound (dead stream, Pulse socket hidden by
      ``ProtectHome``), so "playing" would be a lie;
    * no container while the persisted appliance row still says ``playing`` -
      the state outlived the process.

    The mirror case (Pulse streams with no container) is NOT a mismatch:
    Spotify and her own speech both land on the same sink.

    Deliberately not wired into ``aggregate_sources``: that is polled every
    few seconds by every open dashboard, and this costs a ``docker ps`` plus a
    ``pactl`` call. It runs on the stop path, which is rare and is exactly
    where a lie about silence would be noticed.
    """
    try:
        from puffpai import radio_player
        running = radio_player.is_playing()
    except Exception:                                     # pragma: no cover
        return
    try:
        from puffpai.now_playing import _pulse_sink_input_count
        sinks = _pulse_sink_input_count()
    except Exception:                                     # pragma: no cover
        sinks = None

    if running and sinks == 0:
        logger.warning(
            "radio honesty: the puffpai-radio container is running but Pulse "
            "has no sink-inputs - mpv is alive and silent")
        return
    if not running:
        try:
            row = (app_data or _get_app_data()).get_data("mediahub").get("radio")
            status = (row or {}).get("status")
        except Exception:                                 # pragma: no cover
            return
        if status in ("playing", "connecting"):
            logger.warning(
                "radio honesty: no puffpai-radio container but the appliance "
                "row still says %r", status)


def tool_radio_stop(app_data=None):
    """Queue a stop for the dashboard radio player AND stop the server-side
    appliance player (the ``puffpai-radio`` mpv container).

    Play goes through ``radio_player.play()`` (a docker container), so a stop
    that only queued a browser command left that container streaming with no UI
    control — an orphan you could neither stop nor replace, and the state went
    ``idle`` (hiding the button + now-playing) while audio kept going. Killing
    the container here makes EVERY stop path — the deterministic fastpath, the
    LLM ``media_radio_stop`` tool, and the generic stop — actually silence the
    appliance."""
    ad = app_data or _get_app_data()
    # Observe BEFORE we kill anything: after the stop there is nothing left to
    # compare, and a container/Pulse disagreement is exactly the state that
    # makes "what's playing" answer wrong.
    try:
        radio_truth_check(ad)
    except Exception:
        pass
    try:
        from puffpai import radio_player
        radio_player.stop()          # remove the server-side mpv container
    except Exception:
        pass
    try:
        # Also the choke point for the appliance kill, so a
        # stop from ANY surface silences the container. Idempotent with the
        # explicit stop above, which stays so voice still wins if the app-data
        # write path is unavailable.
        ad.mediahub_radio_request("stop")
    except Exception as exc:
        return f"Could not queue the stop: {exc}"

    msg = "Stopping internet radio on the appliance."
    if not radio_display_online(ad):
        msg += (" The dashboard radio player hasn't checked in recently, "
                "so it may already be off.")

    # The queued command only becomes visible state when a kiosk acks it. A
    # voice stop with no dashboard polling therefore left the tile showing the
    # station we had just killed until its state write aged out. The container
    # is already gone above, so report the server-side truth ourselves —
    # exactly what the ack would have written. Computed after the message so
    # the "hasn't checked in" hint still reflects the real display.
    try:
        ad.mediahub_report_radio("idle", source="appliance")
    except Exception:
        pass
    return msg


def tool_sources_overview(app_data=None):
    """Human-readable roll-up of every audio source."""
    ad = app_data or _get_app_data()

    jf_lines = []
    try:
        from puffpai.media_bridge import get_media_bridge, MediaBridgeError
        try:
            for s in get_media_bridge().now_playing():
                if s.now_playing:
                    verb = "paused" if s.is_paused else "playing"
                    jf_lines.append(
                        f"{s.now_playing.display} — {verb} on "
                        f"{s.device_name or s.client_name}")
        except MediaBridgeError as exc:
            jf_lines.append(f"unavailable ({exc})")
    except Exception as exc:
        jf_lines.append(f"unavailable ({exc})")

    src = aggregate_sources(ad)

    lines = ["Audio sources on PuffPAI:"]
    lines.append("- Jellyfin: " + ("; ".join(jf_lines) if jf_lines else "nothing playing"))

    radio = src["radio"]
    status = radio.get("status") or "idle"
    station = (radio.get("station") or {}).get("name")
    if radio.get("stale"):
        lines.append(f"- World Radio: last said {status}"
                     + (f" ({station})" if station else "")
                     + ", but the display has gone quiet — treat as off.")
    elif status == "idle":
        lines.append("- World Radio: idle.")
    else:
        lines.append(f"- World Radio: {status}" + (f" — {station}" if station else "") + ".")

    sp = src["spotify"]
    if sp.get("active"):
        track = sp.get("track") or {}
        title = track.get("title")
        artists = track.get("artists")
        who = f" (cast by {sp['user']})" if sp.get("user") else ""
        if title:
            lines.append(f"- Spotify Connect: {title}"
                         + (f" — {artists}" if artists else "") + who + ".")
        else:
            lines.append(f"- Spotify Connect: connected{who}.")
    else:
        lines.append("- Spotify Connect: nobody connected — "
                     "cast from your own Spotify app to 'PuffPAI'.")

    return "\n".join(lines)
