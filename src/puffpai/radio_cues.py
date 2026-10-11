"""Does this utterance carry an explicit RADIO cue? Decided LOCALLY, offline.

WHY THIS MODULE EXISTS
    A ``put X on`` fastpath must not decide whether an utterance is a radio
    request by ASKING THE CATALOG (calling ``media_hub.search_stations(X)`` and
    claiming the utterance only if a station comes back). That is a privacy
    defect, not a routing detail. ``search_stations`` egresses to the
    radio-browser.org mirrors, so with the ``radio_browser`` door open the
    object of ANY non-blocklisted "put X on" would leave the house:

        "put grandma on"                 -> query 'grandma'
        "put my therapist on"            -> query 'therapist'
        "put dr smith on"                -> query 'dr smith'
        "put my medication reminder on"  -> query 'medication reminder'
        "put the divorce papers on"      -> query 'divorce papers'

    None of those would play anything (the catalog correctly returns nothing),
    so the leak is invisible from the outside. The household text has already
    left the building by the time the fastpath decides not to act on it.

THE RULE
    Free text reaches the external catalog ONLY when the utterance carries an
    explicit radio cue, decided here with no network access at all:

      * a radio NOUN            radio, station, fm, a numeric AM dial, airwaves
      * a known CALL SIGN       from :data:`CALL_SIGNS` below — a static, local
                                allowlist (npr, bbc, kexp, …)
      * the SERVICE name        tunein / world radio, i.e. the user named the
                                catalog they want searched
      * the LAST-PLAYED station the household's own mediahub record

    Everything else declines locally and the turn proceeds exactly as if this
    fastpath did not exist. This is deliberately a fail-CLOSED allowlist
    and not a blocklist: a blocklist of "things that are not radio stations"
    cannot be completed, because the set of nouns a household says out loud is
    open-ended, and every miss is an egress.

WHY NOT A GENERIC CALL-SIGN REGEX
    North American call signs are K/W + 2-3 letters, which is tempting to match
    with ``\\b[kw][a-z]{2,3}\\b``. Against lowercased STT output that matches
    "what", "with", "want", "know", "kids", "wife" — it would re-open the exact
    hole it is meant to close. The allowlist is explicit on purpose; adding a
    station to it is a one-line, reviewable change.

puff puff pass
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: Generic radio vocabulary. "fm" is unambiguous on its own; "am" is NOT (it is
#: the verb "am"), so it only counts next to a dial number — "1010 am".
_RADIO_NOUN_RE = re.compile(
    r"(?:"
    r"\bradio\b|\bstations?\b|\bfm\b|\bairwaves\b|\bshortwave\b"
    r"|\bbroadcast\b|\bicecast\b|\bnpr\b"
    r"|\b\d{2,4}(?:[.,]\d)?\s*(?:am|fm)\b"      # 88.5 fm · 1010 am
    r"|\b(?:am|fm)\s*\d{2,4}(?:[.,]\d)?\b"      # fm 88.5
    r")",
    re.IGNORECASE)

#: The catalog SERVICE named explicitly. "tune in" as two words is deliberately
#: absent — "tune in the guitar" is not a radio request, and treating the bare
#: idiom as a cue would let its object egress.
_RADIO_SERVICE_RE = re.compile(
    r"(?:\btunein\b|\btune-in\b|\bworld\s+radio\b|\bradio\s*browser\b"
    r"|\bradio-browser\b)",
    re.IGNORECASE)

#: Known broadcasters / call signs. LOCAL, static, conservative. Every entry
#: here is a string a household is overwhelmingly unlikely to use to refer to a
#: person, a medication or a document. Deliberately EXCLUDED for that reason:
#: bare "heart", "kiss", "magic", "smooth", "capital", "virgin", "planet" —
#: they are real station brands but they are also ordinary English, and a false
#: positive here is an egress.
CALL_SIGNS = (
    # US public radio / well-known stations
    "npr", "kexp", "kcrw", "wnyc", "wqxr", "kqed", "kuow", "wbez", "wamu",
    "wbur", "kpcc", "wfmu", "kalx", "kzsu", "kcsm", "wxpn", "kutx", "kboo",
    "jazz24", "jazz 24", "somafm", "soma fm", "radio paradise",
    # UK / Ireland
    "bbc", "bbc radio", "world service", "classic fm", "absolute radio",
    "talksport", "lbc", "rte", "raidio", "6 music", "radio 4", "radio 3",
    "radio 2", "radio 1",
    # Other national broadcasters
    "cbc radio", "triple j", "abc classic", "abc jazz", "rnz",
    "deutschlandfunk", "deutsche welle", "france inter", "france musique",
    "rai radio", "nrk", "sveriges radio",
    # Satellite / aggregator brands
    "iheart", "iheartradio", "siriusxm", "sirius xm",
)

_CALL_SIGN_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    + "|".join(re.escape(s).replace(r"\ ", r"\s+") for s in CALL_SIGNS)
    + r")(?![a-z0-9])",
    re.IGNORECASE)

#: A remembered station name shorter than this is too generic to trust as a
#: cue ("mix", "one"), so it is ignored rather than used to open the door.
_MIN_REMEMBERED = 4


def _remembered_station(app_data=None) -> str | None:
    """The station the household last tuned, read from LOCAL app data.

    ``media_hub`` keeps no recents or favourites list — the mediahub doc has a
    single-slot ``radio.station`` record (``app_data.py``,
    ``mediahub_report_radio``), which is the only "we have played this before"
    memory the appliance has. Never raises, never touches the network.
    """
    try:
        if app_data is None:
            from puffpai.app_data import AppData
            app_data = AppData()
        doc = app_data.get_data("mediahub") or {}
    except Exception as exc:
        logger.debug("mediahub read for radio-cue check failed: %s", exc)
        return None
    if not isinstance(doc, dict):
        return None
    radio = doc.get("radio")
    if not isinstance(radio, dict):
        return None
    station = radio.get("station")
    if isinstance(station, dict):
        name = str(station.get("name") or "").strip()
    elif isinstance(station, str):
        name = station.strip()
    else:
        return None
    return name or None


def has_radio_cue(text: str, app_data=None) -> bool:
    """True when *text* explicitly asks for RADIO. Offline; never raises.

    This is the gate that decides whether household speech may be sent to the
    external station catalog. It must stay cheap (it runs on the fastpath of
    every "put … on" turn) and it must stay fail-closed: when in doubt, False.
    """
    low = (text or "").strip().lower()
    if not low:
        return False
    if _RADIO_NOUN_RE.search(low) or _RADIO_SERVICE_RE.search(low):
        return True
    if _CALL_SIGN_RE.search(low):
        return True
    try:
        remembered = _remembered_station(app_data)
    except Exception as exc:                              # pragma: no cover
        logger.debug("remembered-station cue check failed: %s", exc)
        remembered = None
    if remembered and len(remembered) >= _MIN_REMEMBERED:
        if remembered.strip().lower() in low:
            return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  THE REMAINDER GATE
# ══════════════════════════════════════════════════════════════════════════════
#
# ``has_radio_cue`` asks "did the utterance say RADIO?". That is necessary but
# not sufficient: a household noun sitting NEXT to a real cue would still
# egress, because the cue opens the door for the whole utterance.
#
#     "put on my therapist's radio show"  -> search_stations("therapist's show")
#     "put on grandma's radio show"       -> search_stations("grandma's show")
#     "put on grandma's station"          -> search_stations("grandma's")
#     "play a station for grandma"        -> search_stations("grandma")
#
# The cue is genuinely present in every one of those. What would leak is the
# REMAINDER — what is left once the cue words are taken away — and the
# remainder is exactly where a household puts names, relationships and health
# words.
#
# So the gate is two-stage now: the utterance must carry a cue (above) AND the
# remainder must be built only out of a locally known, closed vocabulary. Every
# token of the remainder must be one of
#
#     * a cue / filler word            radio, station, fm, the, my, please ...
#     * a known call sign or brand     CALL_SIGNS, the local allowlist
#     * a genre tag                    GENRE_WORDS + the caller's tag table
#     * a numeric dial                 90.3 · 90.3 fm · 1010 am
#     * the last-played station name   the household's own mediahub record
#     * a bare number                  "radio 4", "top 40"
#
# and at least one of them must be CONTENT (a call sign, a dial, a tag, or the
# remembered station). Anything else — one unknown token is enough — declines
# locally with :data:`STATION_QUERY_HELP` and makes no network call at all.
#
# When the only content is a genre, the caller must use the TAG path
# (``search_stations_by_tag``) and pass the tag WORD, never the user's free
# text: the tag vocabulary is closed, so a tag search cannot carry household
# words even by accident.

#: Spoken when the remainder is not something we can safely search. It names
#: the three shapes that ARE safe, so the next try succeeds.
STATION_QUERY_HELP = ("Which station? Say a call sign like NPR or KEXP, "
                      "a genre like jazz, or a dial like 90.3 FM.")

#: Genres / formats the catalog indexes as TAGS. Closed, local, and EXTENDED
#: (never replaced) by whatever tag table the caller passes in. Every entry is
#: a format word — none of them is a person, a relationship or a health term.
GENRE_WORDS = (
    "jazz", "blues", "classical", "classic", "rock", "pop", "country", "folk",
    "bluegrass", "hip hop", "hip-hop", "hiphop", "rap", "reggae", "soul",
    "funk", "disco", "electronic", "techno", "house", "trance", "edm",
    "metal", "punk", "indie", "latin", "salsa", "gospel", "oldies",
    "instrumental", "piano", "acoustic", "r&b", "rnb", "ambient", "lo-fi",
    "lofi", "chillout", "chill", "relax", "relaxing", "focus", "study",
    "sleep", "workout", "dance", "christmas", "holiday", "world", "music",
    "news", "talk", "sports", "comedy", "hits", "top 40", "easy listening",
    "60s", "70s", "80s", "90s", "2000s",
)

#: Words that are part of ASKING for radio rather than part of the answer.
#: Dropping them is what leaves the "remainder" this gate judges.
_CUE_WORDS = frozenset("""
radio radios station stations fm am tunein tune-in tune in on to the a an and
some any my our your please just now right stream streaming streams channel
channels live listen listening play playing put start starting up of for
broadcast broadcasts icecast airwaves shortwave dial frequency hd digital
internet online browser radio-browser
""".split())

#: A dial carrying a decimal point ("90.3", "90.3fm") — numeric, never
#: household text, so it counts as content on its own.
_DIAL_DECIMAL_RE = re.compile(r"^\d{2,3}[.,]\d\s?(?:fm|am)?$", re.IGNORECASE)
#: A whole-number dial is content only NEXT TO a band word ("1010 am"), so a
#: bare "1999" stays a song title and is never treated as a frequency.
_DIAL_WHOLE_RE = re.compile(r"^\d{2,4}\s?(?:fm|am)$", re.IGNORECASE)
_BARE_NUMBER_RE = re.compile(r"^\d{1,4}(?:st|nd|rd|th)?$", re.IGNORECASE)
#: A trailing recipient phrase ("... for grandma") is never part of a station
#: name, so it is DROPPED before classification and never forwarded. The genre
#: tags the caller derives come from the whole utterance, so nothing is lost.
_FOR_WHOM_RE = re.compile(r"\bfor\s+.*$", re.IGNORECASE)
#: Keep "." (dials), "'" (so possessives stay visibly UNKNOWN), "-" and "&"
#: (lo-fi, r&b). Everything else is a separator.
_QUERY_PUNCT_RE = re.compile(r"[^\w\s.'&-]")
_TOKEN_RE = re.compile(r"\S+")


def _normalize_query(text: str) -> str:
    low = _QUERY_PUNCT_RE.sub(" ", (text or "").lower())
    low = _FOR_WHOM_RE.sub(" ", low)
    return " ".join(low.split())


def _phrase_re(words):
    """A word-boundaried alternation over *words*, longest phrase first."""
    ordered = sorted({w.lower() for w in words if w}, key=len, reverse=True)
    if not ordered:
        return re.compile(r"(?!x)x")                       # matches nothing
    return re.compile(
        r"(?<![a-z0-9])(?:"
        + "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in ordered)
        + r")(?![a-z0-9])", re.IGNORECASE)


def _cover(raw: str, pattern, mask: list) -> list:
    """Mark every character of *raw* matched by *pattern*.

    Returns the matches as ``(start, text)`` so a caller can rebuild a query
    out of the allowlisted spans ALONE, in the order they were said.
    """
    found = []
    for match in pattern.finditer(raw):
        found.append((match.start(), match.group(0)))
        for i in range(match.start(), match.end()):
            mask[i] = True
    return found


def _dedupe(matches) -> tuple:
    """The matched words, lowercased, in order, without repeats."""
    return tuple(dict.fromkeys(text.lower() for _, text in sorted(matches)))


def plan_station_query(query: str, tag_words=(), app_data=None):
    """How — if at all — may *query* reach the external station catalog?

    Returns ``(kind, value)``:

    ``("name", <query>)``  every token is allowlisted and at least one is a
                           call sign, a dial or the remembered station, so this
                           exact string may be name-searched.
    ``("tag", (word,))``   the only content is genre: use the TAG search with
                           these closed-vocabulary words, never the free text.
    ``("empty", ())``      nothing but cue words — the caller may fall back to
                           tags derived from the utterance, or decline.
    ``("reject", ())``     something unrecognised. Decline locally, speak
                           :data:`STATION_QUERY_HELP`, make no call at all.

    Offline and side-effect free apart from one local app-data read. Never
    raises: an internal failure returns ``("reject", ())`` — it fails CLOSED.
    """
    try:
        raw = _normalize_query(query)
        if not raw:
            return ("empty", ())

        mask = [False] * len(raw)
        signs = _cover(raw, _CALL_SIGN_RE, mask)
        vocab = _phrase_re(tuple(GENRE_WORDS) + tuple(tag_words or ()))
        tags = _cover(raw, vocab, mask)
        remembered = (_remembered_station(app_data) or "").strip().lower()
        known_station = []
        if len(remembered) >= _MIN_REMEMBERED:
            known_station = _cover(raw, _phrase_re((remembered,)), mask)

        dials = []
        unknown = False
        for token in _TOKEN_RE.finditer(raw):
            word = token.group(0)
            if all(mask[i] for i in range(token.start(), token.end())):
                continue                                   # a known phrase
            if word in _CUE_WORDS:
                continue
            tail = raw[token.end():].split()
            following = tail[0].lower() if tail else ""
            if _DIAL_DECIMAL_RE.match(word) or _DIAL_WHOLE_RE.match(word) \
                    or (_BARE_NUMBER_RE.match(word)
                        and following in ("fm", "am")):
                dials.append((token.start(), word))
                continue
            if _BARE_NUMBER_RE.match(word):
                continue                                   # "radio 4", "top 40"
            # An unrecognised token. It is DROPPED, never forwarded, and never
            # logged — it is precisely the household text this gate exists for.
            unknown = True
            logger.debug("station query: dropping an unrecognised token")

        named = signs + dials + known_station
        if not unknown:
            # Nothing unrecognised was said, so the whole remainder is safe as
            # written — which keeps compound brands intact ("bbc radio 4").
            if named:
                return ("name", raw)
            if tags:
                return ("tag", _dedupe(tags))
            return ("empty", ())
        # Something unrecognised was said. Whatever leaves the house is
        # rebuilt from the allowlisted spans ONLY, so the unknown token cannot
        # ride along: "kexp seattle" searches "kexp"; "grandma's station"
        # keeps nothing at all and is declined below.
        if named:
            return ("name", " ".join(t for _, t in sorted(named)))
        if tags:
            return ("tag", _dedupe(tags))
        return ("reject", ())
    except Exception as exc:                               # pragma: no cover
        logger.warning("station query gate failed, declining: %s", exc)
        return ("reject", ())
