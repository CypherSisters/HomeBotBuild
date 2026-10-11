"""Egress doors — the one place that says what may leave this house.

Every outbound call this appliance makes to the public internet is a **door**.
A door has a name, a fixed set of hosts, a stated purpose, a plain-English
description of *what data leaves*, and an owner-controlled flag that is **off
until the owner turns it on**. Nothing in ``src/puffpai`` may reach the WAN
except through a door.

Why this module exists
----------------------
A consent gate for web search (``config.web_search_consent``), an egress
budget for the Toolsmith (``Budget.allow_egress``) and a master chat opt-in
(``chat_allow_egress``) are not enough on their own: any call site gated by
nothing but ``get_network_status().has_internet()`` would reach the internet
while ``/system/status`` reports ``chat_allow_egress:false``. An
enumerated-egress contract that does not enumerate every door is not a
contract; it is a claim.

The invariants
--------------
1. **Default shut.** Every door in :data:`DOORS` declares ``default=False``.
   There is no door that ships open, and :func:`egress_allowed` returns False
   for a door whose flag is absent, junk, or unreadable.
2. **No socket before the decision.** :func:`egress_request` resolves the door
   and validates the host *before* importing ``requests`` or touching the
   network. In particular it runs before any ``has_internet()`` connectivity
   probe — that probe itself opens TCP connections to public resolvers, so a
   "are we online?" check placed ahead of the consent check is already egress.
   A shut door is refused before even a DNS query: the flag decision
   (:func:`require_open`) runs ahead of :func:`_check_url`, which is the first
   thing here that looks a name up.
3. **Unknown host refused, and the ADDRESS behind it checked.** The URL's host
   must be one this door declares — a door is a named destination, not a
   permission to fetch whatever a caller (or a redirect, or an LLM) supplies.
   The declared name is then resolved and every address it answers with must be
   globally routable (:mod:`puffpai.security.netaddr`). A host check that is
   a string comparison and nothing else is safe only while *we* choose the
   hostnames. A door whose host comes from somewhere else
   — a pack manifest, a config edit — is a door whose author owns both sides of
   that comparison, and ``feed.example.invalid A 192.168.1.1`` passes it while
   the card still says nothing about you leaves. The connection is then
   **pinned** to a validated address, so a resolver that answers public for our
   lookup and private for the HTTP client's gets no second answer.
4. **Redirects re-validated per hop.** ``requests`` is called with
   ``allow_redirects=False`` and the hops are followed here, each one checked
   against the same host set. A door to ``api.weather.gov`` that follows a
   302 to somewhere else is not a door to ``api.weather.gov``.
5. **Honest refusal.** When a door is shut the caller gets
   :class:`EgressBlocked`, whose ``user_message`` names the door and how to
   open it. Callers must show that text. Fabricating the data, or silently
   answering from RAG instead, is the failure this whole module exists to make
   impossible to do by accident.

Enforcement tiers
-----------------
``Door.enforcement`` says who actually stops the call:

``registry``
    This module does. The call site uses :func:`egress_get` /
    :func:`egress_request` and cannot reach the wire with the door shut.
``external``
    An equally strong gate that already existed and is not worth duplicating:
    the Toolsmith's per-request egress budget plus its per-hop SSRF validator
    (``agents/toolsmith.py``), and the web-search consent flag. These doors are
    listed so the owner sees them; :func:`egress_allowed` still answers for
    them where a flag exists.
``none``
    **Enumerated but not gated by anything in this registry.** No built-in
    door is in this tier. It stays so that an outbound path that cannot be
    gated yet is listed with ``enforced: false`` rather than hidden, which
    would reproduce exactly the bug this module was written for.

Config
------
Flags live in a top-level ``egress_doors`` object in
``config/puffpai_config.json``::

    "egress_doors": {
      "weather": true,
      "air_quality": true,
      "news_rss": true
    }

Resolution order matches :func:`puffpai.agents.preferences.flag` — environment
first (so an operator can force a door shut from a root-owned unit file without
racing the JSON the running service also reads), then config, then the door's
``default``, which is always ``False``.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from puffpai.logging_config import get_logger
from puffpai.security import netaddr

logger = get_logger(__name__)

#: Config section holding the per-door flags.
CONFIG_SECTION = "egress_doors"

#: How many redirect hops a door will follow, each re-validated against the
#: door's own host set. Three is more than any door here legitimately needs.
MAX_REDIRECTS = 3

#: Default per-request timeout when a caller does not supply one. A door that
#: hangs is a door that stalls the dashboard poll behind it.
DEFAULT_TIMEOUT = 10.0

#: The only port a door may reach. Every door is https and the registry has no
#: way to express a port, so a URL naming any other one is a URL the door's
#: declaration never covered — ``https://api.weather.gov:8123/`` is a request to
#: a service on port 8123 that happens to share a hostname, and the declaration
#: the owner read said nothing about it.
ALLOWED_PORT = 443

#: The ledger's ``reason_code`` for "the declared host resolved to an address
#: this appliance may not open a socket to". One code, a closed vocabulary, so
#: an owner UI (and a grep) can find every one of them; which KIND of address it
#: was travels beside it in ``address_class``. The address itself is never
#: recorded: a resolved RFC 1918 address is a fact about the household's own
#: network and belongs in no log this module writes.
REASON_RESOLVED_PRIVATE = "resolved_private"

#: The ledger's ``reason_code`` for "the declared host does not resolve at all".
#: Separate from the above because "we could not look it up" and "we looked it
#: up and it pointed indoors" are different events for whoever reads the trail.
REASON_UNRESOLVABLE = "unresolvable_host"

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")

#: Plain-English answer to "who actually stops this call", published verbatim by
#: ``/system/status``. A door nothing enforces must SAY so in words an owner
#: reads, not only as ``enforced: false`` in a JSON field beside eleven others.
ENFORCEMENT_NOTES: dict[str, str] = {
    "registry": "Enforced: with the switch off the call is refused before any "
                "socket opens.",
    "derived": "Enforced: this door has no hosts of its own and can only "
               "reach a host some other door you opened already contacts.",
    "external": "Enforced at the call site: the code that opens the socket "
                "asks this switch first and refuses before any socket opens, "
                "then applies its own further checks on top. The refusal "
                "happens there rather than in this registry's request "
                "wrapper, because that code talks to the network through a "
                "library or a separate process (the dashboard server) this "
                "wrapper cannot wrap.",
    "none": "NOT ENFORCED. This door is listed so you can see it, but nothing "
            "in this registry stops the call today: the switch does not shut "
            "it. Treat it as an outbound path that is open whenever the "
            "feature is configured.",
}


class EgressBlocked(RuntimeError):
    """A call was refused before any socket opened.

    ``user_message`` is written to be shown to a person verbatim. It names the
    door and how to open it, because the alternative — a generic failure the
    caller papers over with invented data — is the behaviour that made the
    egress contract untrue in the first place.
    """

    def __init__(self, door_id: str, reason: str, user_message: str):
        super().__init__(f"egress refused [{door_id}]: {reason}")
        self.door_id = door_id
        self.reason = reason
        self.user_message = user_message


@dataclass(frozen=True)
class Door:
    """One named outbound destination and the terms it opens on."""

    id: str
    #: Exact hostnames, lower-case, no ports. A host not in this tuple is
    #: refused even when the door is open.
    hosts: tuple[str, ...]
    #: What the household gets by opening it.
    purpose: str
    #: What leaves the house when it is open. Written for a person, not a
    #: protocol diagram: this string is what the owner reads before deciding.
    data_sent: str
    #: Key under ``egress_doors`` in config/puffpai_config.json.
    config_flag: str
    #: Environment override (wins over config, so a unit file can force shut).
    env_var: str
    #: How consent is given: ``owner_flag``, ``owner_flag_third_party`` (a
    #: named third party receives the request), ``owner_flag_and_agent_budget``
    #: (the owner's standing switch AND the individual request's budget, both
    #: required), or ``derived_from_open_doors``.
    consent_kind: str
    #: Who stops the call: ``registry`` | ``external`` | ``none``. See module
    #: docstring.
    enforcement: str
    #: Sentence shown to the user when the door is shut.
    settings_hint: str
    #: Whose service this is, when it is not the site named in ``hosts``.
    third_party: str | None = None
    #: ALWAYS False. Present so the invariant is a value a test can assert on
    #: rather than a promise in a comment.
    default: bool = False
    #: True for a door whose hosts are chosen per request (the Toolsmith, the
    #: dashboard's page reader).
    dynamic_hosts: bool = False
    #: True for a door that has no hosts of its own and can only reach a host
    #: some OTHER open door already discloses to. Its state is derived from
    #: those doors (see :func:`probe_targets`), and its flag is a hard off
    #: switch rather than an on switch — the only door where an absent flag
    #: does not mean "shut", because with every other door shut it reaches
    #: nothing at all.
    derived: bool = False
    #: True for a door that is declared to reach the household's OWN network
    #: rather than the internet. Its hosts must resolve to RFC 1918 or
    #: unique-local addresses and must NOT resolve to a public one — the mirror
    #: image of every other door, so that "this talks to your router" cannot
    #: quietly become "this talks to the internet" by way of a DNS answer.
    #: **No door declares it today.** It exists so the address posture is a
    #: field with a tested meaning rather than an assumption every reader has
    #: to make, and so the first LAN door cannot be added without one.
    lan: bool = False
    #: Consulted only when ``config_flag`` is absent, so an owner who already
    #: opted in under the old key is not silently opted back out.
    legacy_config_key: str | None = None
    #: False for a door whose hosts the connectivity probe must NEVER borrow,
    #: open or not. For a door that is meant to reach its hosts only during a
    #: rare, owner-scheduled job (the monthly map download), a liveness ping
    #: every few minutes would be contact the owner never agreed to.
    probe_ok: bool = True
    notes: str = ""


#: The doors. This tuple is the enumerated-egress contract. Adding an outbound
#: call to this codebase means adding a row here; ``tests/test_egress_door_registry.py``
#: fails the build if a new ``requests``/``urllib``/``httpx`` call appears
#: outside the registry.
DOORS: tuple[Door, ...] = (
    Door(
        id="weather",
        hosts=("api.open-meteo.com", "api.weather.gov"),
        purpose="Daily forecast and active National Weather Service alerts.",
        data_sent="Approximate latitude and longitude of the house (or the "
                  "place name you set), and nothing else.",
        config_flag="weather",
        env_var="PUFFPAI_EGRESS_WEATHER",
        consent_kind="owner_flag",
        enforcement="registry",
        settings_hint="Weather needs the open-meteo/weather.gov door, which is "
                      "off. Turn it on in Settings > Egress.",
        notes="Also asked by the Node-RED flows daily_weather, "
              "daily_briefing and weather_alerts, if you import them: each "
              "one asks the backend (GET /system/egress/door/weather) before "
              "every call to api.weather.gov and skips the call unless the "
              "answer is a clear yes. The backend refuses to start those "
              "flows while this switch is off.",
    ),
    Door(
        id="air_quality",
        hosts=("air-quality-api.open-meteo.com",),
        purpose="US AQI, pollutants, pollen and the asthma/mold breakdown on "
                "the weather card.",
        data_sent="Approximate latitude and longitude of the house.",
        config_flag="air_quality",
        env_var="PUFFPAI_EGRESS_AIR_QUALITY",
        consent_kind="owner_flag",
        enforcement="registry",
        settings_hint="Air quality needs the open-meteo air-quality door, "
                      "which is off. Turn it on in Settings > Egress.",
    ),
    Door(
        id="news_rss",
        hosts=("feeds.npr.org", "feeds.bbci.co.uk", "news.ycombinator.com",
               "feeds.arstechnica.com", "www.theguardian.com",
               "www.theverge.com"),
        purpose="Headlines for the news widget, from the six feeds in "
                "FEED_DIRECTORY.",
        data_sent="Nothing but the request itself — no query, no household "
                  "data. The publisher learns this house's IP address and that "
                  "it asked for their feed.",
        config_flag="news_rss",
        env_var="PUFFPAI_EGRESS_NEWS_RSS",
        consent_kind="owner_flag",
        enforcement="registry",
        settings_hint="News needs the RSS-feeds door, which is off. Turn it "
                      "on in Settings > Egress.",
        notes="Also asked by the Node-RED flows daily_news and "
              "daily_briefing, if you import them: each one asks the backend "
              "(GET /system/egress/door/news_rss) before fetching the feed in "
              "its NEWS_RSS_URL setting and skips the fetch unless the answer "
              "is a clear yes. That feed is not one of the hosts above; "
              "unless you set another, it is rss.nytimes.com. The backend "
              "refuses to start those flows while this switch is off.",
    ),
    Door(
        id="geocode",
        hosts=("nominatim.openstreetmap.org",),
        purpose="Turn a place name into coordinates for the weather-alert tool.",
        data_sent="The place name being looked up — which for this appliance "
                  "is usually where the household lives.",
        config_flag="geocode",
        env_var="PUFFPAI_EGRESS_GEOCODE",
        consent_kind="owner_flag",
        enforcement="registry",
        settings_hint="Looking up coordinates needs the OpenStreetMap geocoding "
                      "door, which is off. Turn it on in Settings > Egress, or "
                      "set your coordinates directly so nothing has to leave.",
        notes="Kept separate from the weather door on purpose: weather sends "
              "coordinates you already chose, geocoding sends the place name "
              "itself to a third party. Different disclosure, different switch.",
    ),
    Door(
        id="geoip",
        hosts=("ipapi.co",),
        purpose="Guess the household's location from its public IP address "
                "when the user asks for 'here' and no location is stored.",
        data_sent="Your public IP address, by the act of connecting. ipapi.co "
                  "learns roughly where this house is.",
        config_flag="geoip",
        env_var="PUFFPAI_EGRESS_GEOIP",
        consent_kind="owner_flag",
        enforcement="registry",
        settings_hint="Detecting your location from your IP address needs the "
                      "ipapi.co door, which is off. Turn it on in "
                      "Settings > Egress, or set your location once with "
                      "'set my location to ...' so nothing has to leave.",
        notes="Stays OFF by default, including after a config migration. "
              "The stored "
              "location covers the family's actual use; this door exists only "
              "for the 'detect it for me' path and discloses the house's IP to "
              "a company nobody in the household chose.",
    ),
    Door(
        id="radio_browser",
        hosts=("all.api.radio-browser.info", "de1.api.radio-browser.info"),
        purpose="Look up internet radio stations for the World Radio player.",
        data_sent="The station or genre being searched for and, when the "
                  "dashboard player starts a station, that station's id "
                  "(radio-browser counts plays to rank its list).",
        config_flag="radio_browser",
        env_var="PUFFPAI_EGRESS_RADIO_BROWSER",
        consent_kind="owner_flag",
        enforcement="registry",
        settings_hint="Finding radio stations needs the radio-browser door, "
                      "which is off. Turn it on in Settings > Egress. Stations "
                      "already saved still play.",
        notes="radio-browser ONLY. TuneIn has its own switch "
              "('tunein' below), because one "
              "'yes' to a volunteer database is not a 'yes' to a commercial "
              "one. Opening this door sends nothing to TuneIn. The "
              "dashboard's own radio routes (/api/radio/search and "
              "/api/radio/click) ask this switch through the backend before "
              "any request and contact only the hosts listed here. They do "
              "not pin the looked-up address the way this registry's wrapper "
              "does; the hosts are fixed and chosen by this appliance.",
    ),
    Door(
        id="tunein",
        hosts=("opml.radiotime.com",),
        purpose="Find local AM/FM call letters and the stations radio-browser "
                "does not carry, for the World Radio player.",
        data_sent="The station name or genre you asked for, sent to TuneIn at "
                  "opml.radiotime.com — a commercial company, not a volunteer "
                  "database, and not one anybody in this house picked. Like "
                  "any request, it also shows TuneIn this house's public IP "
                  "address. Nothing else about the household goes with it. "
                  "Radio works with this shut: radio-browser still does the "
                  "searching, and stations you already saved still play.",
        config_flag="tunein",
        env_var="PUFFPAI_EGRESS_TUNEIN",
        consent_kind="owner_flag",
        enforcement="registry",
        third_party="TuneIn (Radio Time), a commercial radio-directory company",
        settings_hint="Looking on TuneIn needs the 'tunein' door, which is "
                      "off. Turn it on in Settings > Egress. Radio still works "
                      "without it: radio-browser does the searching, and saved "
                      "stations still play.",
        notes="Split out of the radio_browser door on purpose. "
              "Same capability — find a station to tune — "
              "but not the same disclosure, so it does not get to share the "
              "same switch; see the geocode door for the precedent ('different "
              "disclosure, different switch'). Consulted only when a NAME "
              "search comes back with little or nothing from radio-browser. "
              "With it shut, the reply says TuneIn was NOT looked at rather "
              "than implying it was searched and found nothing. The "
              "dashboard's TuneIn routes (/api/tunein/search and "
              "/api/tunein/resolve) ask this switch through the backend "
              "before any request and contact only the host listed here. A "
              "station whose stream TuneIn names as a playlist on the "
              "broadcaster's own server is skipped rather than fetched, "
              "because that server is not a host this switch names. Those "
              "routes do not pin the looked-up address the way this "
              "registry's wrapper does.",
    ),
    Door(
        id="web_search",
        hosts=("duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com",
               "links.duckduckgo.com"),
        purpose="Ad-hoc web search from a chat turn, via the ddgs library.",
        data_sent="The search query, verbatim — to DuckDuckGo on the chat "
                  "tool's path, and to whichever engines your own SearXNG "
                  "relays to on the Toolsmith's. Whatever the person typed "
                  "that led to the search leaves the house either way.",
        config_flag="web_search",
        env_var="PUFFPAI_EGRESS_WEB_SEARCH",
        consent_kind="owner_flag",
        enforcement="external",
        legacy_config_key="web_search_consent",
        # Names the door id, not one engine: this switch now covers the chat
        # tool's DuckDuckGo path AND the Toolsmith's SearXNG one, and an owner
        # told only about DuckDuckGo would not know what they had turned off.
        settings_hint="Searching the web needs the 'web_search' egress door, "
                      "which is off. Turn it on in Settings > Egress.",
        notes="Enforced at TWO call sites, both of which ask "
              "egress_allowed('web_search') before a socket opens: "
              "assistant_tools.web_search (ddgs opens its own sockets, so "
              "egress_request cannot wrap them; the gate runs before ddgs is "
              "imported) and agents/toolsmith._web_search. The Toolsmith's "
              "first hop is the owner's own SearXNG on loopback, which is not "
              "itself egress — but that container then queries upstream "
              "engines on the household's behalf, so the query leaves the "
              "house all the same and belongs behind the same switch. Gating "
              "it here means one switch turns off ALL web search, which is "
              "what an owner means when they turn web search off. Honours the "
              "pre-existing top-level web_search_consent key when the new flag "
              "is absent, so an owner who already opted in is not silently "
              "opted back out.",
    ),
    Door(
        id="web_fetch",
        hosts=(),
        dynamic_hosts=True,
        purpose="The Toolsmith reading a public page the owner or a planned "
                "run named.",
        data_sent="The URL requested. The Toolsmith is the one agent that reads "
                  "attacker-controlled text, and it holds no household "
                  "credential.",
        config_flag="web_fetch",
        env_var="PUFFPAI_EGRESS_WEB_FETCH",
        consent_kind="owner_flag_and_agent_budget",
        enforcement="external",
        settings_hint="Reading a web page needs the 'web_fetch' egress door, "
                      "which is off. Turn it on in Settings > Egress.",
        notes="Enforced at the call site in agents/toolsmith.py, which asks "
              "egress_allowed('web_fetch') at the top of _web_fetch and again "
              "at the top of _fetch_url (the one hop loop both fetch paths, "
              "web_fetch and search_and_read, reach the wire through), "
              "BEFORE any URL shape check, DNS resolution or socket. Not "
              "rerouted through egress_request because this door has no fixed "
              "host list to validate against and the Toolsmith's own gates "
              "are stricter than the wrapper's: a per-request "
              "Budget(allow_egress) that defaults False, plus per-hop SSRF "
              "validation that refuses loopback, link-local, private and "
              "IPv6-mapped addresses and re-validates every redirect before a "
              "socket opens. Both are required: this "
              "switch is the household's standing consent, the budget is the "
              "individual request's, and neither substitutes for the other.",
    ),
    Door(
        id="page_reader",
        hosts=(),
        dynamic_hosts=True,
        purpose="Show a web page someone opens from the dashboard as plain "
                "text in the in-app reader, instead of leaving the screen.",
        data_sent="The address of the page, sent to the site that hosts it. "
                  "That site also learns this house's public IP address. No "
                  "cookies, no referring page and no browser name that "
                  "identifies the house go with it.",
        config_flag="page_reader",
        env_var="PUFFPAI_EGRESS_PAGE_READER",
        consent_kind="owner_flag",
        enforcement="external",
        settings_hint="Opening web pages in the reader is switched off. To "
                      "turn it on, open Settings and switch on 'Web page "
                      "reader' in the Bridge section.",
        notes="Enforced at the call site in the dashboard server: /api/reader "
              "asks the backend whether this switch is open "
              "(GET /system/egress/door/page_reader) before it looks up or "
              "connects to anything, and refuses when the answer is no, "
              "missing or unreadable. With the switch on, the fetch still "
              "refuses every loopback, private and link-local address, "
              "re-checks each redirect, connects to the address it checked, "
              "and hands back plain text only. Kept apart from web_fetch on "
              "purpose: that switch is the Toolsmith's standing consent to "
              "read pages for a request or a planned run, this one is a "
              "person at the screen opening a link. Different reader, "
              "different switch.",
    ),
    Door(
        id="tv_free_channels",
        # The one fixed host is Pluto TV's anonymous-session call. The video
        # itself comes from whatever hosts the channel's own playlists name
        # (stitchers, CDNs, ad servers), so the rest is per request.
        hosts=("boot.pluto.tv",),
        dynamic_hosts=True,
        purpose="Playing the free internet channels the owner set aside "
                "(mostly Pluto TV) on the kiosk screen, or casting them to a "
                "TV in the house. For a cast the box fetches the channel and "
                "relays it, so the TV never holds the session.",
        data_sent="For Pluto TV: a request for an anonymous viewing session "
                  "(no account, no name), which Pluto ties to the house's "
                  "public IP address and rough location, the way the Pluto app "
                  "on any TV does. Then, for every free channel: requests for "
                  "that channel's video and ads from the servers its playlist "
                  "names, from the house's IP address.",
        config_flag="tv_free_channels",
        env_var="PUFFPAI_EGRESS_TV_FREE_CHANNELS",
        consent_kind="owner_flag_third_party",
        enforcement="external",
        third_party="Pluto TV (Paramount) and the free channels' video and ad "
                    "servers",
        settings_hint="Free TV channels are switched off. To watch or cast "
                      "them, open Settings and switch on 'Free TV channels' "
                      "in the Bridge section.",
        notes="Enforced at two call sites. tv_cast.py calls "
              "require_open(\"tv_free_channels\") before the Pluto session "
              "request and before EVERY relayed playlist or segment fetch, so "
              "closing the switch stops a cast that is already running, and "
              "records its hops in the egress ledger by host. A live channel "
              "fetches a segment every few seconds, so within one cast each "
              "host and outcome is written the first time, then at most once "
              "every 10 minutes with a count of the fetches in between, and "
              "any count still open is written when the cast ends. Not "
              "routed through egress_request because the video hosts come from "
              "the channel's playlists; tv_cast instead uses netaddr's pinned "
              "PUBLIC-posture session (every address checked, LAN and loopback "
              "refused), https on port 443 only, and re-validates each "
              "redirect hop. The Pluto session token stays in the API "
              "process's memory: it is never logged, never written to disk, "
              "and never sent to Home Assistant or the TV (both only see an "
              "opaque /cast/<id> link that dies on Stop). tv_player.py asks "
              "the same switch before it starts the kiosk player for a free "
              "channel, refuses while it is closed, and records each start in "
              "the ledger. Before the container starts it checks only the "
              "channel's own address from the list (https, and not an address "
              "inside the house). While this switch is open, the player "
              "container then fetches the playlist's own redirects and "
              "segments itself, on the host network, with no address check, "
              "no https check and no ledger line, so a playlist could point "
              "it at a device on your network. The relay used for casting "
              "checks every hop; the kiosk player does not. Closing the "
              "switch does not end a channel already playing; Stop does.",
    ),
    Door(
        id="map_data_updates",
        # Never borrowed by the connectivity probe, whatever else is open:
        # these hosts are contacted only during an actual refresh run.
        probe_ok=False,
        hosts=("download.geofabrik.de", "download1.graphhopper.com"),
        purpose="Downloads new map data for the region this box was built "
                "for (the Texas example region unless you built another) "
                "about once a month so directions stay current. Your "
                "questions and trips never go out.",
        data_sent="Only requests for the region's public map files: the road map "
                  "from download.geofabrik.de and the place list from "
                  "download1.graphhopper.com. Like any download, it shows "
                  "those two sites this house's public IP address. No place "
                  "you asked about, no trip and no home location goes with it. "
                  "Directions keep working with this shut, on the map data "
                  "already on the box.",
        config_flag="map_data_updates",
        env_var="PUFFPAI_EGRESS_MAP_DATA_UPDATES",
        consent_kind="owner_flag",
        enforcement="registry",
        third_party="Geofabrik (road map files) and GraphHopper (place files), "
                    "both publishing OpenStreetMap data",
        settings_hint="Map data updates is switched off. Directions still "
                      "work with the map data already on this box. To get "
                      "newer maps, open Settings and switch on "
                      "'map_data_updates' in the Bridge section.",
        notes="The ONLY caller is "
              "puffpai.maps_refresh, run from a nightly puffpai cron entry "
              "(deploy/maps/refresh.sh) that acts only when an update is "
              "due, about every 28 days. It streams the two files through "
              "egress_request (every redirect re-checked against these two "
              "hosts), checks each against the .md5 published beside it on "
              "the same host, then hands them to deploy/maps/build_release.sh "
              "and activate_release.sh, which never touch the live maps until "
              "a new build has passed its checks. With the switch off the job "
              "contacts nobody and records OFF. GET and HEAD requests only; no "
              "credential, cookie or identifier is sent (User-Agent "
              "PuffPAI-maps/1.0).",
    ),
    Door(
        id="knowledge_pack_downloads",
        # Never borrowed by the connectivity probe: these hosts are contacted
        # only when the owner has confirmed one download.
        probe_ok=False,
        # Kiwix's own servers, then the official mirrors from the mirror list
        # download.kiwix.org publishes, then the three non-Kiwix rows of
        # config/source_registry.json. source_fetch.KIWIX_HOSTS and the
        # registry rows are checked against this tuple by the tests.
        hosts=("download.kiwix.org", "lb.download.kiwix.org",
               "mirror.download.kiwix.org", "dumps.wikimedia.org",
               "ftp.nluug.nl", "ftpmirror.your.org",
               "mirror-sites-in.mblibrary.info", "ny.mirror.driftle.ss",
               "wi.mirror.driftle.ss",
               "api.nasa.gov", "docs.astropy.org", "medlineplus.gov"),
        purpose="Download a knowledge pack the owner picked from the vetted "
                "source list, with the checksum Kiwix publishes for it, so "
                "the library can be read offline afterwards.",
        data_sent="Only requests for the public pack files the owner "
                  "confirmed, their checksums and the sites' robots.txt. Like "
                  "any download, it shows those sites this house's public IP "
                  "address. No question, document or household data goes "
                  "with it.",
        config_flag="knowledge_pack_downloads",
        env_var="PUFFPAI_EGRESS_KNOWLEDGE_PACK_DOWNLOADS",
        consent_kind="owner_flag_third_party",
        enforcement="registry",
        third_party="Kiwix (download.kiwix.org) and its official mirrors; "
                    "NASA, Astropy and MedlinePlus for their three rows",
        settings_hint="Knowledge pack downloads is switched off. The packs "
                      "already on this box still work. To download one, open "
                      "Settings and switch on 'knowledge_pack_downloads' in "
                      "the Bridge section, or copy the pack onto this box "
                      "yourself.",
        notes="The ONLY caller is puffpai.source_fetch (the setup wizard's "
              "Integrate step and setup_ops.fetch_source), and only for a "
              "row of the shipped source registry whose license, size and "
              "address the owner confirmed. Every hop goes through "
              "egress_request with follow_redirects=False, so source_fetch "
              "applies its own rule on each redirect: a Kiwix download may go "
              "download.kiwix.org, then lb.download.kiwix.org, then one "
              "listed mirror serving that same path, and nowhere else. The "
              "file is checked against the .sha256 lb.download.kiwix.org "
              "publishes for it before it is installed.",
    ),
    Door(
        id="connectivity_probe",
        hosts=(),
        purpose="offline_guard.has_internet() — 'is there a WAN link at all?', "
                "asked before an online feature reports itself offline.",
        data_sent="Nothing this door chooses. The probe opens a TCP "
                  "connection to a host that is ALREADY behind a door you "
                  "turned on, so it can tell you nothing a door you opened "
                  "does not already tell that same host. With every door shut "
                  "it contacts nobody and answers 'offline'.",
        config_flag="connectivity_probe",
        env_var="PUFFPAI_EGRESS_CONNECTIVITY_PROBE",
        consent_kind="derived_from_open_doors",
        enforcement="derived",
        settings_hint="",
        derived=True,
        notes="It has no hosts of its own. Probing fixed public hosts such "
              "as 1.1.1.1, 8.8.8.8 or www.google.com would contact parties "
              "nobody in the household chose, on an appliance that never "
              "needs the cloud. Instead probe_targets() returns hosts drawn "
              "from the doors that are open right now, so a fully shut "
              "appliance opens zero external "
              "sockets. The flag is a hard OFF switch, not an on switch: set "
              "it false and has_internet() stops probing entirely and reports "
              "offline. Leaving it unset does not open anything: the open "
              "doors decide.",
    ),
    Door(
        id="model_pull",
        hosts=("registry.ollama.ai",),
        purpose="Download a language model into Ollama on this box — the "
                "setup wizard's 'get the brain' step, and nothing else.",
        data_sent="The name of the model being downloaded, and this house's IP "
                  "address by the act of connecting. No household data, no "
                  "question anyone asked. Gigabytes come back the other way.",
        config_flag="model_pull",
        env_var="PUFFPAI_EGRESS_MODEL_PULL",
        consent_kind="owner_flag_third_party",
        enforcement="external",
        third_party="Ollama (registry.ollama.ai)",
        # Names the config key and the env var, not Settings > Egress: this
        # door is not in _FLAG_HONOURED_EXTERNAL, so door_status() reports it
        # writable:false and the Settings card shows its switch disabled.
        settings_hint="Downloading a model needs the 'model_pull' egress door, "
                      "which is off. Turn it on by setting "
                      "egress_doors.model_pull to true in "
                      "config/puffpai_config.json (or "
                      "PUFFPAI_EGRESS_MODEL_PULL=1 in the environment), or run "
                      "`ollama pull <model>` yourself from a machine that is "
                      "already online.",
        notes="Enforced at the call site, brain_health.pull_model, which asks "
              "egress_allowed('model_pull') before the POST. It cannot go "
              "through egress_request because OUR socket goes to Ollama at "
              "config `ollama.url` — loopback, or the LAN address the owner "
              "configured — and it is OLLAMA that then fetches from "
              "registry.ollama.ai. The box egresses even though this process "
              "does not, which is exactly why the row says registry.ollama.ai "
              "and not the Ollama URL: the door names where the data actually "
              "goes. The wizard offers the download only with this door open "
              "and prints the manual command otherwise.",
    ),
    Door(
        id="embedding_model_download",
        hosts=("chroma-onnx-models.s3.amazonaws.com",),
        purpose="Fetch the sentence-embedding model files ChromaDB needs, once "
                "(~80 MB). Without them the knowledge library can be listed "
                "and read but not searched by meaning.",
        data_sent="Nothing but the request itself — no query, no household "
                  "data. Amazon S3 learns this house's IP address and that it "
                  "asked for a public model file.",
        config_flag="embedding_model_download",
        env_var="PUFFPAI_EGRESS_EMBEDDING_MODEL_DOWNLOAD",
        consent_kind="owner_flag_third_party",
        enforcement="external",
        third_party="Amazon S3 (ChromaDB's public model bucket)",
        # Not a Settings control either (see model_pull), so the hint names the
        # switch that is.
        settings_hint="The embedding model files are not on this box and the "
                      "'embedding_model_download' door is off. Turn it on by "
                      "setting egress_doors.embedding_model_download to true "
                      "in config/puffpai_config.json (or "
                      "PUFFPAI_EGRESS_EMBEDDING_MODEL_DOWNLOAD=1 in the "
                      "environment), or copy ~/.cache/chroma over from another "
                      "machine and nothing has to leave at all.",
        notes="Enforced at the call site, embeddings._chroma_default_ef, which "
              "checks whether the files are already on disk and asks "
              "egress_allowed('embedding_model_download') only when they are "
              "not. chromadb opens its own socket for this download, so "
              "egress_request cannot wrap it; the gate runs before the "
              "embedding function is constructed. Once the files are cached "
              "this door is never consulted again, and a box that got them by "
              "sneakernet never opens it at all. The same check runs again on "
              "every call of the built function, and before the knowledge "
              "search's degraded fallback (used when the configured embedder "
              "fails) lets chroma embed a question with a collection's own "
              "function: with the files absent and this door shut, that "
              "fallback is skipped rather than allowed to download.",
    ),
    Door(
        id="spotify",
        hosts=("accounts.spotify.com", "api.spotify.com"),
        purpose="Control Spotify by voice and from the dashboard: connect the "
                "Spotify account, then play, pause, skip and search on the "
                "appliance's own Spotify speaker.",
        data_sent="The Spotify app credentials and account token you "
                  "connected, what is being played or searched for, and the "
                  "playback commands. Spotify also learns this house's public "
                  "IP address.",
        config_flag="spotify",
        env_var="PUFFPAI_EGRESS_SPOTIFY",
        consent_kind="owner_flag",
        enforcement="registry",
        third_party="Spotify AB",
        settings_hint="Spotify is switched off. To use it, open Settings and "
                      "switch on 'Spotify' in the Bridge section.",
        notes="Covers every call spotify_control.py makes: connecting the "
              "account, refreshing its token, and each Web API request. With "
              "the switch off none of them is looked up or dialled, and the "
              "reply says Spotify is switched off. Not covered: the librespot "
              "containers (docker-compose.spotify.yml and "
              "docker-compose.spotify-voice.yml). They are optional services "
              "you start yourself, and while one runs it signs in to Spotify "
              "and streams from it on its own, whatever this switch says. "
              "This switch cannot stop them; stopping the container is the "
              "switch for that path.",
    ),
)

_BY_ID: dict[str, Door] = {d.id: d for d in DOORS}
_HOST_INDEX: dict[str, Door] = {
    host: d for d in DOORS for host in d.hosts
}


# ── config + flag resolution ─────────────────────────────────────────────────

_lock = threading.Lock()
_cache: dict | None = None

#: Repo root — this file is ``src/puffpai/security/egress_doors.py``. Only used
#: to build the in-tree fallback for :func:`overlay_path` when
#: ``$PUFFPAI_DATA_DIR`` is unset (a dev checkout).
_REPO_ROOT_FOR_OVERLAY = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def _config_path() -> str | None:
    """Absolute path to puffpai_config.json, or None if unavailable.

    Through :func:`puffpai.config.resolve_config_dir`: the ``doors`` section is
    the OWNER's declaration of what this build may reach, and an owner who edits
    the config of a relocated install must be editing the file this reads. The
    old ``<repo>/config`` literal read the shipped copy instead, so a widened
    door read as shut and a narrowed one read as the shipped default.
    """
    try:
        from puffpai.config import resolve_config_dir
        return os.path.join(resolve_config_dir(), "puffpai_config.json")
    except Exception as exc:  # egress policy must never gate startup
        logger.debug("config path unavailable for egress doors: %s", exc)
        return None


def _raw_config() -> dict:
    """The whole config file, read once and cached. Never raises.

    Read directly rather than through the config dataclass because unknown keys
    are dropped there, and a door added by a build must resolve without the
    loader having been taught about it.
    """
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        raw: dict = {}
        path = _config_path()
        if path:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    raw = loaded
            except (OSError, ValueError) as exc:
                # Fail shut: an unreadable config yields no open doors.
                logger.warning(
                    "egress door config unreadable (%s): %s — every door stays "
                    "shut", path, exc)
        _cache = raw
        return raw


#: Basename of the owner overlay. NOTE: ``egress_doors._ledger_path()`` uses
#: the SAME basename for the last-used snapshot, in the sibling
#: ``security_logs/`` directory. They are different files with different jobs:
#: this one is owner INTENT (which doors may open), that one is TELEMETRY (when
#: each door last fired). The ``kind`` field below exists so a reader that has
#: been pointed at the wrong one can say so.
OVERLAY_NAME = "egress_doors.json"

#: Marks a file as an owner overlay rather than the last-used snapshot.
OVERLAY_KIND = "egress_door_overlay"

#: Directory mode for the overlay's parent, and file mode for the overlay. It
#: records what the household chose to expose itself to; it is not for every
#: account on the appliance.
OVERLAY_DIR_MODE = 0o750
OVERLAY_FILE_MODE = 0o640

#: Sentinel for "the overlay has not been read yet", so that a cached ``None``
#: (the file is there and broken) is distinguishable from "not looked yet".
_OVERLAY_UNSET = object()
_overlay_cache: object = _OVERLAY_UNSET

#: Test/ops hook, same shape as ``egress_ledger.set_path_override``.
_OVERLAY_OVERRIDE: str | None = None


def set_overlay_override(path: str | None) -> None:
    """Point the overlay at *path* (tests, and an operator moving the file)."""
    global _OVERLAY_OVERRIDE
    with _lock:
        _OVERLAY_OVERRIDE = path
    reset_cache()


def overlay_path() -> str:
    """Absolute path to the owner overlay.

    Lands under ``$PUFFPAI_DATA_DIR`` — the one tree a hardened unit
    (``ProtectSystem=strict``) actually grants the service. The appliance
    config lives in ``/opt/puffpai/config``, which is deliberately NOT in
    ``ReadWritePaths``: an owner flipping a switch must not require giving the
    API server write access to the whole appliance configuration, because an
    RCE in the API would inherit it. The overlay is one small file of booleans,
    so the blast radius of that write permission is the door flags themselves.
    """
    if _OVERLAY_OVERRIDE:
        return _OVERLAY_OVERRIDE
    from puffpai.statedir import resolve_runtime_dir
    return os.path.join(
        resolve_runtime_dir(os.path.join(_REPO_ROOT_FOR_OVERLAY, "state"),
                            env_subdir="state"),
        OVERLAY_NAME)


def _raw_overlay() -> dict | None:
    """The overlay's ``doors`` map, ``{}`` if there is none, ``None`` if broken.

    The three answers are distinct on purpose:

    * ``{}`` — no overlay file yet. Normal on a fresh box; the config file and
      then the code default decide, exactly as before this file existed.
    * a dict — the owner's explicit per-door settings.
    * ``None`` — the file is there and will not parse. Treated by
      :func:`_resolve_flag` as "every door shut", matching how an unreadable
      config is already handled. A file we cannot read is not consent.
    """
    global _overlay_cache
    with _lock:
        cached = _overlay_cache
    if cached is not _OVERLAY_UNSET:
        return cached  # type: ignore[return-value]
    # Resolved outside the lock: overlay_path() reaches puffpai.statedir, which
    # may import puffpai.config, and holding this lock across that import is
    # how a lock-ordering deadlock gets built.
    path = overlay_path()
    result: dict | None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
    except FileNotFoundError:
        result = {}
    except (OSError, ValueError) as exc:
        logger.warning(
            "egress door overlay unreadable (%s): %s — every door stays shut",
            path, exc)
        result = None
    else:
        if not isinstance(loaded, dict):
            logger.warning("egress door overlay %s is not an object — every "
                           "door stays shut", path)
            result = None
        else:
            doors = loaded.get("doors")
            if doors is None and loaded.get("kind") != OVERLAY_KIND:
                # Most likely the last-used SNAPSHOT, which shares this
                # basename one directory over. Say so rather than failing shut
                # on telemetry an operator mis-pointed us at.
                logger.warning(
                    "egress door overlay %s has no 'doors' object; ignoring it "
                    "(is this the security_logs snapshot of the same name?)",
                    path)
                result = {}
            elif not isinstance(doors, dict):
                logger.warning("egress door overlay %s has a non-object "
                               "'doors' — every door stays shut", path)
                result = None
            else:
                result = doors
    with _lock:
        _overlay_cache = result
    return result


def reset_cache() -> None:
    """Forget the cached config and overlay (tests, and a reload after an edit)."""
    global _cache, _overlay_cache
    with _lock:
        _cache = None
        _overlay_cache = _OVERLAY_UNSET


def _as_bool(value) -> bool | None:
    """Interpret a config/env value as a flag, or None if it is not one."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return None


# ── app doors: a manifest's declared hosts, registered as real doors ─────────
#
# An installed app's ``doors[]`` block is not only schema-checked at load:
# these functions register a manifest's hosts as real doors, which is what
# lets ``hosts_enforcement`` say more than the weaker ``"declared_only"``.
#
# Four properties, and each of them is why this lives HERE rather than in
# ``app_registry``:
#
# 1. **Forced shut.** ``default`` is hard-coded False below, never read from the
#    manifest. There is no manifest key that opens a door; the only way one
#    opens is the owner's own ``POST /system/egress/door``, which writes the
#    overlay tier. An app cannot open its own door, and neither can its author.
# 2. **Namespaced.** Every id is ``app:<app_id>:<n>``. Built-in ids are
#    ``[a-z_]+`` with no colon, so a manifest can neither collide with nor
#    shadow ``weather`` or ``radio_browser``; a collision is refused outright
#    rather than resolved.
# 3. **The consent flag is bound to the hosts it was given for.** The config
#    flag carries an 8-hex digest of the door's sorted host set. An app update
#    that repoints a door the owner already opened produces a DIFFERENT flag
#    key, which no tier has an opinion on, so the door falls back to
#    ``default`` — shut. Consent does not survive a redefinition of what it was
#    consent to. The precedence chain itself is untouched: env > overlay >
#    config > default, exactly as for a built-in door.
# 4. **Same enforcement, not a parallel one.** An app door is an ordinary
#    ``Door`` with ``enforcement="registry"``, so :func:`app_egress_request`
#    is just :func:`egress_request`: shut-door refusal before DNS, exact-host
#    allowlist, 443 only, https only, every resolved address checked, redirects
#    re-validated, and one content-free ledger line.

#: Every app door id starts with this. A built-in id can never contain a colon.
APP_DOOR_PREFIX = "app:"

#: An app id, mirrored from ``app_registry._ID_RE``. Re-stated rather than
#: imported: this module must not depend on the registry it protects, and a
#: door id becomes an env var name and a config key.
_APP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: A hostname a manifest may declare. Mirrors ``app_registry._HOST_RE`` and is
#: applied AGAIN here, because "the loader already checked it" is not a control
#: this module is willing to inherit for the one field it enforces on.
_APP_HOST_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)"
                          r"(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))*$")

#: The last label of a registrable NAME. Alphabetic, or the ``xn--`` ACE form
#: of an internationalised TLD — either way it **begins with a letter**, and
#: that is the load-bearing property: no IP literal in any base or shorthand
#: can end in a label that starts with a letter.
_APP_TLD_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")


def _is_ip_literal(host: str) -> bool:
    """True if *host* is an address rather than a name, in ANY notation.

    ``_APP_HOST_RE`` alone let four of these through, because a dotted quad is
    a perfectly legal hostname shape: ``8.8.8.8`` matched, registered, and once
    the owner opened the door, a request to that address went straight to the
    transport. A door is supposed to name a HOST, whose addresses this module
    then resolves and judges; a literal skips the name entirely and lets a
    manifest point at an address directly.

    Every notation ``inet_aton`` and the C resolver accept has to be caught,
    not just the dotted quad:

    * dotted quad — ``8.8.8.8``, ``127.0.0.1``
    * octal / zero-padded — ``010.010.010.010``
    * hex — ``0x08080808``, ``0x7f.1``
    * a bare integer — ``2130706433`` is 127.0.0.1
    * shorthand with fewer than four parts — ``127.1``, ``10.1.2``
    * IPv6 in every form, bracketed or not, including IPv4-mapped —
      ``::1``, ``[::1]``, ``fd00::1``, ``::ffff:127.0.0.1``

    Done with parsing rather than a pattern, because the set of things that
    parse as an address is defined by the parsers, not by a regex somebody
    hoped was complete.
    """
    if not isinstance(host, str):
        return False
    text = host.strip()
    if not text:
        return False
    # Brackets are the IPv6 URL form and nothing else.
    if text.startswith("[") or text.endswith("]") or ":" in text:
        return True
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        pass
    # The legacy IPv4 notations ``ipaddress`` deliberately refuses but the
    # platform resolver still honours. Each part may be decimal, octal (leading
    # zero) or hex (0x), and fewer than four parts is legal shorthand.
    parts = text.split(".")
    if len(parts) > 4:
        return False
    for part in parts:
        if not part:
            return False
        low = part.lower()
        try:
            if low.startswith("0x"):
                int(low, 16)
            elif low.startswith("0") and len(low) > 1:
                int(low, 8)
            else:
                int(low, 10)
        except ValueError:
            return False  # a non-numeric part means this is a name
    return True


def _is_registrable_hostname(host: str) -> bool:
    """True only for a NAME a door may declare. Refuses every address literal.

    Three conditions, and the second and third are what stop an address
    literal from passing as a name:

    1. it matches the hostname shape (``_APP_HOST_RE``);
    2. it has at least two labels, so a single bare token — ``localhost``,
       ``2130706433``, ``0x08080808`` — is never a door host. A public-posture
       door has no legitimate use for an unqualified name;
    3. its last label is TLD-shaped, i.e. it starts with a letter. This is the
       one rule that kills every dotted numeric form at once: ``8.8.8.8`` and
       ``127.1`` end in a label that is not a TLD.

    :func:`_is_ip_literal` then runs as well. Belt and braces on purpose: rule
    3 is the reasoning, the parser is the proof, and neither is trusted alone.
    """
    if not isinstance(host, str) or not _APP_HOST_RE.match(host):
        return False
    if _is_ip_literal(host):
        return False
    labels = host.split(".")
    if len(labels) < 2:
        return False
    return bool(_APP_TLD_RE.match(labels[-1]))

#: The most doors one app may register, and the most hosts one of them may
#: name. Deliberately EQUAL to ``app_registry.MAX_DOORS`` / ``MAX_DOOR_HOSTS``
#: rather than lower: a stricter ceiling here would silently drop doors from a
#: manifest the loader accepted, and "the app declared a door that does not
#: exist" is a confusing failure even though it fails shut. Re-stated rather
#: than imported so that raising the loader's ceiling is a deliberate act here
#: too — this module must not inherit a limit it did not agree to.
MAX_APP_DOORS_PER_APP = 12
MAX_APP_DOOR_HOSTS = 16

_APP_LOCK = threading.RLock()
_APP_DOORS: dict[str, Door] = {}


def app_door_id(app_id: str, index: int) -> str:
    """The registry id for one manifest door: ``app:<app_id>:<n>``."""
    return f"{APP_DOOR_PREFIX}{app_id}:{int(index)}"


def _app_host_digest(hosts: tuple[str, ...]) -> str:
    """8 hex of sha256 over the sorted host set — the consent binding.

    Not a security boundary against a collision-hunting attacker (it is 32
    bits, and it does not need to be): the flag key is not a secret and nothing
    is authenticated by it. Its job is that a host set the owner never saw
    cannot inherit a ``true`` the owner wrote for a different one, and for that
    an accidental collision is the only case that matters.
    """
    payload = "\n".join(sorted(hosts))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def app_door_flag(app_id: str, index: int, hosts) -> str:
    """The config/overlay key for one manifest door."""
    clean = tuple(sorted({_clean_host(h) for h in (hosts or ()) if _clean_host(h)}))
    return f"app_{app_id}_{int(index)}_{_app_host_digest(clean)}"


def _app_env_var(flag: str) -> str:
    """``PUFFPAI_EGRESS_APP_...`` — the operator's hard override for an app door."""
    return "PUFFPAI_EGRESS_" + flag.upper()


def _app_door_from_manifest(app_id: str, index: int, block) -> Door | None:
    """Build one ``Door`` from one manifest ``doors[]`` entry, or None.

    Returns None — never a partially-trusted door — for anything it cannot
    read. A door this function refuses to build is a door that does not exist,
    which is the fail-shut outcome: the app reaches nothing.
    """
    if not isinstance(block, dict):
        return None
    raw_hosts = block.get("hosts")
    if not isinstance(raw_hosts, list):
        return None
    hosts: list[str] = []
    for raw in raw_hosts[:MAX_APP_DOOR_HOSTS]:
        host = _clean_host(raw)
        # Re-check the shape here. A wildcard, a scheme, a port or a path in
        # what the loader handed us must never become an entry in an allowlist
        # that ``_check_url`` compares against with ``in`` — and neither must
        # an IP literal, which would let a manifest name an ADDRESS and skip
        # the name-resolution this door layer exists to judge.
        if not host or not _is_registrable_hostname(host):
            return None
        if host not in hosts:
            hosts.append(host)
    purpose = block.get("purpose")
    data_sent = block.get("data_sent_plain")
    door_key = app_door_id(app_id, index)
    flag = app_door_flag(app_id, index, hosts)
    return Door(
        id=door_key,
        hosts=tuple(hosts),
        purpose=(str(purpose)[:400] if isinstance(purpose, str) else ""),
        data_sent=(str(data_sent)[:400] if isinstance(data_sent, str) else ""),
        config_flag=flag,
        env_var=_app_env_var(flag),
        consent_kind="owner_flag",
        enforcement="registry",
        # A door that names no host gets no switch on the Settings card
        # (_is_inert), so its sentence must not send the owner looking there.
        settings_hint=(
            (f"The '{app_id}' app's door is off. Turn it on in "
             f"Settings > Egress if you want this appliance talking to "
             f"{', '.join(hosts)}.") if hosts else
            (f"The '{app_id}' app's door is off and names no host: the app "
             f"declares that it reaches nowhere, so there is nothing to turn "
             f"on.")),
        # NEVER from the manifest. Stated as a literal so that the invariant is
        # a value the tests assert on rather than a promise in a comment.
        default=False,
        notes=(f"Declared by the '{app_id}' app manifest and registered on "
               f"rescan. Shut until the owner opens it; a host this door does "
               f"not name is refused even when it is open."),
    )


def register_app_doors(entries) -> tuple[str, ...]:
    """Replace the whole app-door set from *entries*: ``[(app_id, doors), ...]``.

    A wholesale replace, not a merge, because a rescan IS the install and
    uninstall mechanism: an app whose folder is gone must lose its doors in the
    same breath, and a merge would leave a removed app's door in the table with
    the owner's old ``true`` still resolving against it.

    Returns the ids registered, in order. Never raises: a registration fault
    must not take the appliance's door table down with it, and the fail-shut
    outcome of a refused entry is simply that no door exists for it.
    """
    built: dict[str, Door] = {}
    order: list[str] = []
    for entry in (entries or ()):
        try:
            app_id, doors = entry
        except (TypeError, ValueError):
            continue
        app_id = str(app_id or "").strip().lower()
        if not _APP_ID_RE.match(app_id):
            logger.warning("egress: refusing app doors for unusable app id %r",
                           str(app_id)[:64])
            continue
        if not isinstance(doors, list):
            continue
        for index, block in enumerate(doors[:MAX_APP_DOORS_PER_APP]):
            d = _app_door_from_manifest(app_id, index, block)
            if d is None:
                logger.warning("egress: app %r door %d is not registrable; the "
                               "app reaches nothing through it", app_id, index)
                continue
            if d.id in _BY_ID or d.id in built:
                # Cannot happen while ids are namespaced and indexed, which is
                # exactly why it is checked: the day the naming changes, a
                # manifest must not be able to shadow a built-in door.
                logger.warning("egress: refusing duplicate app door id %r", d.id)
                continue
            built[d.id] = d
            order.append(d.id)
    with _APP_LOCK:
        _APP_DOORS.clear()
        _APP_DOORS.update(built)
    logger.info("egress: %d app door(s) registered, all shut by default: %s",
                len(order), ", ".join(order) or "-")
    return tuple(order)


def clear_app_doors() -> None:
    """Forget every registered app door (a build with no apps, and tests)."""
    with _APP_LOCK:
        _APP_DOORS.clear()


def app_doors() -> tuple[Door, ...]:
    """The registered app doors, in registration order."""
    with _APP_LOCK:
        return tuple(_APP_DOORS.values())


# ── skill doors ─────────────────────────────────────────────────────
#
# Skills get their OWN table, lock and flag namespace rather than sharing the
# app one. The reason is mechanical and it is the whole point: ``register_app_
# doors`` is a WHOLESALE replace (``_APP_DOORS.clear()``), because an app rescan
# IS the install/uninstall mechanism. Skills rescan on their own schedule --
# an install, a disable, a removal -- and if the two shared a table, one
# subsystem's rescan would silently delete the other's doors and leave the
# owner's ``true`` flags resolving against nothing. Two tables, two clears,
# ``all_doors()`` merges. ``tests/test_skills_registry.py`` pins that an app
# rescan leaves skill doors standing and vice versa.

SKILL_DOOR_PREFIX = "skill:"

_SKILL_LOCK = threading.RLock()
_SKILL_DOORS: dict[str, Door] = {}


def skill_door_id(skill_id: str, index: int = 0) -> str:
    """The registry id for one skill's door: ``skill:<skill_id>:<n>``."""
    return f"{SKILL_DOOR_PREFIX}{skill_id}:{int(index)}"


def skill_door_flag(skill_id: str, index: int, hosts) -> str:
    """The config/overlay key for one skill door: ``skill_<id>_<n>_<digest>``.

    Digest-bound to the host set exactly as an app door's flag is: the owner's
    ``true`` is for the hosts that were on the consent card, so a skill that is
    re-proposed with an extra host gets a different key and therefore a shut
    door until the owner approves the new card.
    """
    clean = tuple(sorted({_clean_host(h) for h in (hosts or ()) if _clean_host(h)}))
    return f"skill_{skill_id}_{int(index)}_{_app_host_digest(clean)}"


def _skill_env_var(flag: str) -> str:
    """``PUFFPAI_EGRESS_SKILL_...`` — the operator's hard override."""
    return "PUFFPAI_EGRESS_" + flag.upper()


def _skill_door_from_manifest(skill_id: str, index: int, block) -> Door | None:
    """Build one skill ``Door``, or None. Same refusals as the app builder."""
    if not isinstance(block, dict):
        return None
    raw_hosts = block.get("hosts")
    if not isinstance(raw_hosts, list) or not raw_hosts:
        return None
    hosts: list[str] = []
    for raw in raw_hosts[:MAX_APP_DOOR_HOSTS]:
        host = _clean_host(raw)
        if not host or not _is_registrable_hostname(host):
            return None
        if host not in hosts:
            hosts.append(host)
    purpose = block.get("purpose")
    data_sent = block.get("data_sent_plain")
    flag = skill_door_flag(skill_id, index, hosts)
    return Door(
        id=skill_door_id(skill_id, index),
        hosts=tuple(hosts),
        purpose=(str(purpose)[:400] if isinstance(purpose, str) else ""),
        data_sent=(str(data_sent)[:400] if isinstance(data_sent, str) else ""),
        config_flag=flag,
        env_var=_skill_env_var(flag),
        consent_kind="owner_flag",
        enforcement="registry",
        settings_hint=(
            f"The '{skill_id}' skill's door is off. Turn it on in "
            f"Settings > Egress if you want this appliance talking to "
            f"{', '.join(hosts)} on its behalf."),
        # NEVER from the package. A skill's door opens only because the owner
        # approved a consent card naming exactly these hosts.
        default=False,
        notes=(f"Declared by the '{skill_id}' skill and registered on rescan. "
               f"Shut until the owner approves it; a host this door does not "
               f"name is refused even when it is open."),
    )


def register_skill_doors(entries) -> tuple[str, ...]:
    """Replace the whole SKILL-door set from ``[(skill_id, doors), ...]``.

    Wholesale, for the app-door reason: a rescan is the install/uninstall
    mechanism, and a removed skill must lose its door in the same breath. Never
    raises — a refused entry simply means that skill reaches nothing.
    """
    built: dict[str, Door] = {}
    order: list[str] = []
    for entry in (entries or ()):
        try:
            skill_id, doors = entry
        except (TypeError, ValueError):
            continue
        skill_id = str(skill_id or "").strip().lower()
        if not _APP_ID_RE.match(skill_id):
            logger.warning("egress: refusing skill doors for unusable id %r",
                           str(skill_id)[:64])
            continue
        if not isinstance(doors, list):
            continue
        for index, block in enumerate(doors[:MAX_APP_DOORS_PER_APP]):
            d = _skill_door_from_manifest(skill_id, index, block)
            if d is None:
                logger.warning("egress: skill %r door %d is not registrable; "
                               "the skill reaches nothing through it", skill_id,
                               index)
                continue
            if d.id in _BY_ID or d.id in built:
                logger.warning("egress: refusing duplicate skill door id %r", d.id)
                continue
            built[d.id] = d
            order.append(d.id)
    with _SKILL_LOCK:
        _SKILL_DOORS.clear()
        _SKILL_DOORS.update(built)
    logger.info("egress: %d skill door(s) registered, all shut by default: %s",
                len(order), ", ".join(order) or "-")
    return tuple(order)


def clear_skill_doors() -> None:
    """Forget every registered skill door (a build with no skills, and tests)."""
    with _SKILL_LOCK:
        _SKILL_DOORS.clear()


def skill_doors() -> tuple[Door, ...]:
    """The registered skill doors, in registration order."""
    with _SKILL_LOCK:
        return tuple(_SKILL_DOORS.values())


def skill_of(d: "Door | str") -> str | None:
    """The skill id that declared this door, or None for any other door."""
    key = d.id if isinstance(d, Door) else str(d)
    if not key.startswith(SKILL_DOOR_PREFIX):
        return None
    rest = key[len(SKILL_DOOR_PREFIX):]
    return rest.split(":", 1)[0] or None


def all_doors() -> tuple[Door, ...]:
    """Every door this registry enforces: built-in, then app, then skill."""
    return DOORS + app_doors() + skill_doors()


def app_of(d: "Door | str") -> str | None:
    """The app id that declared this door, or None for a built-in one."""
    key = d.id if isinstance(d, Door) else str(d)
    if not key.startswith(APP_DOOR_PREFIX):
        return None
    rest = key[len(APP_DOOR_PREFIX):]
    return rest.split(":", 1)[0] or None


def _is_inert(d: Door) -> bool:
    """True for a door whose switch would move nothing: it names no host.

    Every one of the five shipped apps declares ``hosts: []`` — "this app
    reaches nowhere" — and a door with no host refuses every URL whether it is
    open or shut, because ``_check_url`` asks ``host in d.hosts``. Publishing a
    switch for it would put a control in front of an owner that cannot change
    what leaves the house, which is the same dishonesty as a status page that
    reports a door shut while it fires. The door is still LISTED, with its
    purpose and its "nothing" — it just is not offered as a toggle.

    Excludes the two built-in doors that legitimately carry no host tuple: the
    Toolsmith's per-request door and the derived probe.
    """
    return not d.hosts and not d.dynamic_hosts and not d.derived


def door(door_id: str) -> Door | None:
    """The door with this id, or None. Built-in ids win over app ids."""
    key = str(door_id).strip().lower()
    found = _BY_ID.get(key)
    if found is not None:
        return found
    with _APP_LOCK:
        found = _APP_DOORS.get(key)
    if found is not None:
        return found
    with _SKILL_LOCK:
        return _SKILL_DOORS.get(key)


def door_ids() -> tuple[str, ...]:
    """Every door id, in registry order."""
    return tuple(d.id for d in all_doors())


def door_for_host(host: str) -> Door | None:
    """The door that declares *host*, or None if no door does.

    A built-in door wins: an app that declares ``api.weather.gov`` does not get
    to become the door that host is attributed to.
    """
    clean = _clean_host(host)
    found = _HOST_INDEX.get(clean)
    if found is not None:
        return found
    for d in (*app_doors(), *skill_doors()):
        if clean in d.hosts:
            return d
    return None


def _clean_host(host) -> str:
    """Normalise a hostname for comparison: lower-case, no trailing dot."""
    if not isinstance(host, str):
        return ""
    return host.strip().rstrip(".").lower()


def egress_allowed(door_id: str) -> tuple[bool, str]:
    """Is this door open? Returns ``(allowed, reason)``.

    Fails shut on every ambiguity: an unknown door, an unreadable config, a
    junk flag value and a missing flag all return ``False``. *reason* is for
    logs and tests; the sentence to show a person is ``Door.settings_hint``.
    """
    d = door(door_id)
    if d is None:
        return False, f"no egress door named {door_id!r}"

    if d.derived:
        # A derived door owns no hosts. Reporting it "shut" while it probes, or
        # "open" while every other door is closed and it therefore contacts
        # nobody, would both be lies — and a status page that lies about egress
        # is the exact defect this module exists to remove. Ask the probe what
        # it would actually do right now.
        targets, reason = _derived_probe_state(d)
        return bool(targets), reason

    decided, _source, reason = _resolve_flag(d)
    if decided is None:
        return d.default, reason
    return decided, reason


#: The tiers a door's current value can come from, highest precedence first.
#: Published per door by :func:`door_status` as ``source``, because "the switch
#: is off" and "the switch is off BECAUSE the unit file forces it and your
#: click cannot win" are different facts and the owner needs the second one.
FLAG_SOURCES = ("env", "overlay", "config", "default")

#: Doors with ``enforcement="external"`` whose own call site still reads this
#: registry's flag, so the switch is a real control. ``egress_admin`` imports
#: this as the authority; it lives here so ``door_status()`` can publish
#: ``writable`` without importing the writer.
#:
#: Every member is a claim about one specific line of code, and
#: ``tests/test_egress_api.py`` greps the source for that line — so the
#: day a refactor drops the call, the set stops being true out loud instead of
#: quietly leaving an owner with a dead switch:
#:
#: * ``web_search``  → ``assistant_tools.web_search`` calls ``require_open("web_search")``
#:   and ``agents/toolsmith.py`` calls it before reaching SearXNG.
#: * ``web_fetch``   → ``agents/toolsmith.py`` calls ``require_open("web_fetch")``
#:   at the top of ``_web_fetch`` and again at the top of ``_fetch_url`` (the
#:   single hop loop both fetch paths reach the wire through), BEFORE any URL
#:   shape check, DNS resolution or socket. Without that call the only gate
#:   would be the Toolsmith's per-request ``Budget.allow_egress``, which never
#:   reads this registry, and the owner's switch would move nothing but the
#:   status display.
#: * ``page_reader`` → ``dashboard/app/api/reader/route.ts`` calls
#:   ``checkBridgeSwitch("page_reader")`` before ``fetchArticle``, and refuses
#:   unless the backend answers that the switch is open.
#: * ``tv_free_channels`` → ``tv_cast._relay_get`` calls
#:   ``require_open("tv_free_channels")`` before every relayed fetch, and
#:   ``tv_player.free_channels_refusal`` asks it before the kiosk player starts.
#: * ``model_pull``  → ``brain_health.pull_model`` calls
#:   ``egress_allowed("model_pull")`` before its POST to Ollama's ``/api/pull``
#:   (and ``setup_cli._offer_model_pull`` asks too, before it offers).
#: * ``embedding_model_download`` → ``embeddings.chroma_default_may_run`` calls
#:   ``egress_allowed(ONNX_DOWNLOAD_DOOR)``; ``embeddings._chroma_default_ef``
#:   asks it before building chroma's default function (and the function asks
#:   again on every call), and the RAG fallback in
#:   ``api_server._get_rag_context`` asks the same helper before letting chroma
#:   embed text.
_FLAG_HONOURED_EXTERNAL = frozenset({"web_search", "web_fetch", "page_reader",
                                     "tv_free_channels", "model_pull",
                                     "embedding_model_download"})


def _resolve_flag(d: Door) -> tuple[bool | None, str, str]:
    """Resolve one door's flag. Returns ``(value_or_None, source, reason)``.

    The single precedence chain, used by both :func:`egress_allowed` and
    :func:`_raw_flag` so the two can never disagree:

    1. ``env`` — ``PUFFPAI_EGRESS_*``. An operator forcing a door from the
       systemd unit must not be overridable by anything the running service can
       write, which is the whole reason this tier is first.
    2. ``overlay`` — the owner's own switches, in :func:`overlay_path`. This is
       the tier the API writes, because it is the only one a hardened unit
       grants the service.
    3. ``config`` — ``egress_doors`` in puffpai_config.json, then the door's
       ``legacy_config_key``. Shipped/provisioned state.
    4. ``default`` — ``Door.default``, which is False for every door.

    ``None`` for the value means no tier expressed an opinion, so the caller
    applies ``Door.default``. Every ambiguity below resolves to False, never to
    True: a junk value, an unreadable overlay and an unreadable config are all
    "not consent".
    """
    raw_env = os.environ.get(d.env_var)
    if raw_env is not None and raw_env.strip() != "":
        decided = _as_bool(raw_env)
        if decided is None:
            return False, "env", (f"{d.env_var}={raw_env.strip()!r} is not a "
                                  f"flag value; door stays shut")
        return decided, "env", (f"{d.env_var}="
                                f"{'on' if decided else 'off'} "
                                f"(environment wins)")

    overlay = _raw_overlay()
    if overlay is None:
        return False, "overlay", (
            f"the owner overlay at {overlay_path()} will not parse; every "
            f"door stays shut until it is fixed or removed")
    if d.config_flag in overlay:
        decided = _as_bool(overlay.get(d.config_flag))
        if decided is None:
            return False, "overlay", (f"overlay {d.config_flag} is not a flag "
                                      f"value; door stays shut")
        return decided, "overlay", (f"overlay {d.config_flag}={decided} "
                                    f"(set by the owner)")

    section = _raw_config().get(CONFIG_SECTION)
    if isinstance(section, dict) and d.config_flag in section:
        decided = _as_bool(section.get(d.config_flag))
        if decided is None:
            return False, "config", (f"{CONFIG_SECTION}.{d.config_flag} is not "
                                     f"a flag value; door stays shut")
        return decided, "config", f"{CONFIG_SECTION}.{d.config_flag}={decided}"

    if d.legacy_config_key:
        decided = _as_bool(_raw_config().get(d.legacy_config_key))
        if decided is not None:
            return decided, "config", (
                f"legacy key {d.legacy_config_key}={decided} "
                f"(set {CONFIG_SECTION}.{d.config_flag} to move it)")

    return None, "default", (f"{CONFIG_SECTION}.{d.config_flag} is not set; "
                             f"default is {'open' if d.default else 'shut'}")


def flag_source(d: Door) -> str:
    """Which tier decides this door's value right now. One of :data:`FLAG_SOURCES`."""
    return _resolve_flag(d)[1]


def _raw_flag(d: Door) -> bool | None:
    """The owner's explicit setting for this door, or None if they gave none.

    Same chain as :func:`egress_allowed` (it is literally the same function);
    a junk value is treated as "not a flag", which for a normal door means the
    door stays shut and for the derived probe means "no explicit off".
    """
    return _resolve_flag(d)[0]


#: Port a probe connects on. Every door in this registry is https-only, so a
#: probe of an open door's host is the same connection that door already makes.
PROBE_PORT = 443


def probe_targets(door_id: str | None = None) -> tuple[tuple[str, int, str], ...]:
    """Hosts a connectivity probe may contact right now: ``(host, port, door)``.

    The doctrine is "egress only through enumerated doors", and a liveness probe
    is egress. Rather than give the probe hosts of its own — which is how
    ``has_internet()`` came to open TCP to Cloudflare, Google DNS and google.com
    on a box that advertises that it never needs the cloud — it borrows the
    hosts of doors that are **open right now**. Three consequences, all of them
    the point:

    * With every door shut this returns ``()`` and the caller must answer
      "offline" **without opening a socket**. A fully shut appliance contacts
      nobody, including to ask whether it could.
    * A probe can never disclose anything a door the owner opened does not
      already disclose to that same host.
    * Setting ``egress_doors.connectivity_probe`` false is a hard off: no
      probing at all, whatever else is open.

    Pass *door_id* to prefer that door's own hosts — a caller that has already
    passed ``require_open`` for its door should probe that door, not another.
    """
    probe = _BY_ID.get("connectivity_probe")
    if probe is not None and _raw_flag(probe) is False:
        return ()

    out: list[tuple[str, int, str]] = []
    every = all_doors()
    ordered = list(every)
    if door_id:
        preferred = door(door_id)
        if preferred is not None:
            ordered = [preferred] + [d for d in every if d is not preferred]
    for d in ordered:
        if d.derived or d.dynamic_hosts or not d.hosts or not d.probe_ok:
            continue
        allowed, _reason = egress_allowed(d.id)
        if not allowed:
            continue
        for host in d.hosts:
            out.append((host, PROBE_PORT, d.id))
    return tuple(out)


def _derived_probe_state(d: Door) -> tuple[tuple, str]:
    """``(targets, reason)`` for the derived connectivity-probe door."""
    if _raw_flag(d) is False:
        return (), (f"{CONFIG_SECTION}.{d.config_flag}=False — probing is "
                    f"switched off; nothing is contacted and has_internet() "
                    f"reports offline")
    targets = probe_targets()
    if not targets:
        return (), ("no door with a host is open, so the probe contacts "
                    "nobody and has_internet() reports offline")
    host, _port, source = targets[0]
    return targets, (f"derived from the open doors: would reach {host} "
                     f"(door {source}), which that door already contacts")


def require_open(door_id: str) -> Door:
    """Return the door if it is open, else raise :class:`EgressBlocked`.

    Call this at the top of any outbound path whose sockets this module cannot
    wrap (a third-party client library, for instance). It opens nothing.
    """
    allowed, reason = egress_allowed(door_id)
    d = door(door_id)
    if not allowed:
        _record(door_id, ok=False, detail=reason, kind="door_check")
        if d and d.settings_hint:
            message = d.settings_hint
        elif d and d.derived:
            # A derived door is shut by its own flag, which is a hard off and
            # never an on, or because no door with a host is open (see
            # _derived_probe_state).
            if _raw_flag(d) is False:
                message = (f"That needs the '{door_id}' egress door, which its "
                           f"own switch has turned off ({CONFIG_SECTION}."
                           f"{d.config_flag}, env {d.env_var}).")
            else:
                message = (f"That needs the '{door_id}' egress door, which is "
                           f"off because no door with a host is open.")
        elif d:
            # Only a door with no sentence of its own lands here (spotify), and
            # none of those has a switch on the Settings card, so name the flag
            # this check just read.
            message = (f"That needs the '{door_id}' egress door, which is off. "
                       f"Its switch is {CONFIG_SECTION}.{d.config_flag} in "
                       f"config/puffpai_config.json (env {d.env_var}).")
        else:
            message = f"That needs the '{door_id}' egress door, which is off."
        raise EgressBlocked(door_id, reason, message)
    return d  # type: ignore[return-value]


# ── addresses: where a declared name actually points ─────────────────────────

#: Test/ops hook, same shape as ``egress_ledger.set_path_override``. Injected so
#: a test can prove that a NAME resolving into the LAN is refused without owning
#: a domain that does, and so that no test in this suite makes a real DNS query.
_RESOLVER_OVERRIDE = None


def set_resolver_override(resolver) -> None:
    """Point address resolution at *resolver* — ``(host, port) -> [address]``.

    ``None`` restores :func:`puffpai.security.netaddr.default_resolver`.
    """
    global _RESOLVER_OVERRIDE
    _RESOLVER_OVERRIDE = resolver


def _resolver():
    return _RESOLVER_OVERRIDE or netaddr.default_resolver


def _posture(d: Door) -> str:
    """Which address posture this door is judged under."""
    return netaddr.LAN if d.lan else netaddr.PUBLIC


def _refuse_resolved(d: Door, host: str, address_class: str,
                     hop: bool) -> "EgressBlocked":
    """Refusal for a declared name that points somewhere it may not.

    The resolved address is deliberately absent from every string built here.
    The ledger gets the CLASS of address — ``loopback``, ``private``,
    ``link_local`` — which is what an owner needs to understand the refusal;
    the address itself would put the household's own network topology into a
    file that is not thought of as sensitive.
    """
    where = "redirect hop" if hop else "initial url"
    return _refuse(
        d,
        f"{REASON_RESOLVED_PRIVATE} ({address_class}): host {host!r} resolves "
        f"to an address this appliance may not open a socket to ({where})",
        f"That request tried to reach {host}, which points at an address on "
        f"this house's own network rather than the public internet, so nothing "
        f"was sent.",
        host, kind="address_check",
        reason_code=REASON_RESOLVED_PRIVATE, address_class=address_class)


def _check_addresses(d: Door, host: str, hop: bool = False) -> tuple:
    """Resolve *host* and prove EVERY answer is one this door may reach.

    Every answer, not the first: the HTTP client picks which address it dials,
    so a name with a public ``A`` record and a loopback ``AAAA`` record is a
    name that reaches loopback whenever the client prefers IPv6. One bad answer
    refuses the whole name.

    A name that does not resolve is refused too. "We could not look it up" is
    not "it was fine", and a door that fell through to an unvalidated fetch on a
    resolver hiccup would be a door with a weather-dependent gate.
    """
    # The refusal is BUILT in the handler and RAISED after it, never inside.
    # ``AddressRefused``'s own message names the resolved address, and an
    # exception raised inside an ``except`` block carries the one it is handling
    # as ``__context__`` — where anything that walks the chain (a debugger, an
    # error reporter, a future ``logger.exception``) would find that address.
    # ``raise ... from None`` only hides it from the printed traceback; it stays
    # on the object. Raising outside the handler leaves nothing to find.
    refusal: "EgressBlocked | None" = None
    try:
        return netaddr.validated_addresses(
            host, ALLOWED_PORT, posture=_posture(d), resolver=_resolver())
    except netaddr.AddressRefused as refused:
        refusal = _refuse_resolved(d, host, refused.address_class, hop)
    except netaddr.UnparseableAddress:
        # The resolver handed back something that is not an address. Refuse: an
        # answer we cannot check is an answer we cannot trust.
        refusal = _refuse(
            d, f"{REASON_RESOLVED_PRIVATE} (unparseable): the resolver gave "
               f"{host!r} an answer that is not an address",
            f"That request tried to reach {host}, and the answer could not be "
            f"checked, so nothing was sent.",
            host, kind="address_check",
            reason_code=REASON_RESOLVED_PRIVATE, address_class="unparseable")
    except netaddr.ResolutionError:
        # Only the fact, never the resolver's message: a getaddrinfo error
        # string can quote the name and the system's search domains.
        refusal = _refuse(
            d, f"{REASON_UNRESOLVABLE}: host {host!r} does not resolve",
            f"That request tried to reach {host}, which this house's network "
            f"could not look up, so nothing was sent.",
            host, kind="address_check", reason_code=REASON_UNRESOLVABLE)
    raise refusal


def _pinned_session(d: Door):
    """A ``requests`` session that can only dial an address we just validated.

    Separated so a test can substitute a transport, and so the failure to build
    one is a refusal rather than a silent fall-back to an unpinned client: the
    pin is the half of this control that survives a hostile resolver, and a
    caller that believes it is pinned when it is not is worse off than one that
    knows it is not.
    """
    return netaddr.pinned_session(posture=_posture(d), resolver=_resolver())


# ── the wrapper ──────────────────────────────────────────────────────────────

def egress_request(door_id: str, method: str, url: str, *,
                   timeout: float = DEFAULT_TIMEOUT,
                   follow_redirects: bool = True, **kwargs):
    """Make one outbound request through *door_id*, or refuse without a socket.

    Order matters and is the security property:

    1. resolve the door and its flag — nothing has touched the network yet, not
       even a DNS query;
    2. validate the URL's scheme, port and host against the door's declared
       hosts, then resolve that host and prove **every** address it answers
       with is one this door may reach;
    3. only then build a transport and connect — and the transport is **pinned**
       to a validated address, so the client cannot resolve the name a second
       time and get a different answer;
    4. follow at most :data:`MAX_REDIRECTS` hops, re-validating each ``Location``
       against the same host set *and* the same address policy, because a
       redirect target is chosen by the server and the approval that covered
       hop one says nothing about hop two.

    Raises :class:`EgressBlocked` for a shut door, an undeclared host, or a
    declared host that points somewhere this appliance may not go, and lets
    transport errors from ``requests`` propagate to the caller unchanged.

    ``follow_redirects=False`` returns a redirect response to the caller
    instead of following it. The caller then makes the next hop as a new call
    through this function, so every hop still passes steps 1 to 3; it is for a
    caller with a stricter per-hop rule than the door's host set.
    """
    d = require_open(door_id)
    _check_url(d, url)

    kwargs.pop("allow_redirects", None)  # this function owns hop policy
    session = _pinned_session(d)  # built after the decision, never before
    # A session is per request: pooled connections are keyed by host name, and
    # a reused connection is one whose address was validated at some earlier
    # time under some earlier DNS answer. No door here is high enough volume for
    # the extra handshake to matter.
    streaming = bool(kwargs.get("stream"))

    try:
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            pinned_refusal = None
            try:
                resp = session.request(method, current, timeout=timeout,
                                       allow_redirects=False, **kwargs)
            except netaddr.AddressRefused as refused:
                # The pin fired: between our lookup and the socket, the name
                # answered with an address it may not reach. This is the
                # rebinding case, and it is why validating alone is not enough.
                # Built here, raised below the handler — see _check_addresses
                # for why the exception must not carry a __context__.
                pinned_refusal = _refuse_resolved(
                    d, _host_of(current), refused.address_class, hop=_hop > 0)
            except EgressBlocked:
                raise
            except Exception as exc:
                # The door was opened and a socket was attempted: that belongs
                # in the trail even though nothing came back. Only the
                # exception's TYPE is recorded — a requests error message
                # quotes the full URL, query string included.
                host, path = _safe_parts(current)
                _record(door_id, ok=True, detail=f"{method.upper()} {host} "
                                                 f"failed: {type(exc).__name__}",
                        host=host, path=path, kind="request_failed")
                raise
            if pinned_refusal is not None:
                raise pinned_refusal
            if follow_redirects and resp.status_code in (301, 302, 303,
                                                         307, 308):
                location = resp.headers.get("Location") or ""
                # Done with this hop: release its connection now, before the
                # next hop is checked (and possibly refused), so a streamed
                # redirect chain leaks nothing back into the session.
                hop_closer = getattr(resp, "close", None)
                if callable(hop_closer):
                    try:
                        hop_closer()
                    except Exception:  # pragma: no cover - transport specific
                        pass
                nxt = urljoin(current, location)
                # Re-validate before following. A redirect off the door's host
                # set — or onto a host that resolves indoors — is refused here
                # rather than followed and judged afterwards.
                _check_url(d, nxt, hop=True)
                current = nxt
                continue
            host, path = _safe_parts(current)
            _record(door_id, ok=True,
                    detail=f"{method.upper()} {host}",
                    host=host, path=path,
                    bytes_out=_request_bytes(resp),
                    bytes_in=_declared_length(resp),
                    status=getattr(resp, "status_code", None))
            if streaming:
                # The body has not been read yet, so the session must outlive
                # this function. Nothing in the tree streams through a door
                # today; this exists so that the first caller who does gets a
                # working response rather than a closed socket.
                try:
                    resp._puffpai_egress_session = session
                except Exception:  # pragma: no cover - transport specific
                    pass
            return resp
    finally:
        if not streaming:
            closer = getattr(session, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # pragma: no cover - transport specific
                    pass

    _record(door_id, ok=False, detail="too many redirects",
            host=_host_of(current))
    raise EgressBlocked(
        d.id, f"more than {MAX_REDIRECTS} redirects",
        f"{d.settings_hint or 'That request'} — the site redirected too many "
        f"times, so nothing was fetched.")


def egress_get(door_id: str, url: str, **kwargs):
    """``GET`` through a door. See :func:`egress_request`."""
    return egress_request(door_id, "GET", url, **kwargs)


def app_egress_request(app_id: str, index: int, method: str, url: str,
                       **kwargs):
    """The ONE sanctioned outbound path for an installed app. Refuses by default.

    This is :func:`egress_request` against the door the app's manifest declared,
    and it is deliberately nothing more — routing app egress through the same
    wrapper is the whole point, because a second path is a second set of checks
    to get wrong. What the app gets, in order and before any socket:

    * a door that is **shut** unless the owner opened it — and an app that is
      not installed, or a door index its manifest never declared, has no door
      at all, which refuses the same way;
    * its own ``hosts[]`` as an **exact** allowlist. Not a suffix match, not a
      wildcard: ``evil-api.example.com`` does not pass because
      ``api.example.com`` was declared, and an app declaring no host reaches
      nothing at all;
    * https and port 443 only;
    * every address the name resolves to checked against the public posture, so
      a declared name that answers with a LAN address is refused rather than
      dialled, and the socket is pinned to an address that already passed;
    * redirects re-validated against the same host set, at most
      :data:`MAX_REDIRECTS` of them;
    * one ledger line naming the DOOR (``app:<app_id>:<n>``) and the host, with
      the query string already discarded — the same content-free record every
      other door writes.

    ``index`` is the door's position in the manifest's ``doors[]`` list, which
    is what :func:`app_door_id` namespaces on.
    """
    return egress_request(app_door_id(str(app_id).strip().lower(), index),
                          method, url, **kwargs)


def app_egress_get(app_id: str, index: int, url: str, **kwargs):
    """``GET`` through an app's declared door. See :func:`app_egress_request`."""
    return app_egress_request(app_id, index, "GET", url, **kwargs)


def _host_of(url: str) -> str:
    try:
        return _clean_host(urlsplit(url).hostname or "")
    except ValueError:
        return ""


def _safe_parts(url: str) -> tuple[str, str]:
    """``(host, path)`` with the query string discarded, for the ledger."""
    try:
        from puffpai.security.egress_ledger import safe_parts
        return safe_parts(url)
    except Exception:
        return _host_of(url), ""


def _request_bytes(resp) -> int | None:
    """How many bytes of body this appliance SENT, or None if unknowable.

    A length, never the content. GETs send no body and record 0, which is the
    honest answer to "what did we hand over" for a door like ``weather``.
    """
    try:
        body = getattr(getattr(resp, "request", None), "body", None)
        if body is None:
            return 0
        if isinstance(body, (bytes, bytearray)):
            return len(body)
        if isinstance(body, str):
            return len(body.encode("utf-8", "replace"))
    except Exception:
        pass
    return None


def _declared_length(resp) -> int | None:
    """``Content-Length`` of the reply, read from headers only.

    Deliberately does not touch ``resp.content``: reading the body here would
    consume a streamed response the caller has not seen yet.
    """
    try:
        raw = resp.headers.get("Content-Length")
        return int(raw) if raw is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _refuse(d: Door, reason: str, user_message: str, host: str = "",
            kind: str = "url_check", reason_code: str = "",
            address_class: str = "") -> "EgressBlocked":
    """Ledger the refusal, then hand back the exception for the caller to raise.

    A URL refused for its host or its scheme is a door decision like any other
    and belongs in the trail: "the appliance tried to reach somewhere it was not
    allowed to" is the single most interesting line an owner can find.
    """
    _record(d.id, ok=False, detail=reason, host=host, kind=kind,
            reason_code=reason_code, address_class=address_class)
    return EgressBlocked(d.id, reason, user_message)


def _check_url(d: Door, url: str, hop: bool = False) -> None:
    """Refuse anything the door does not cover.

    Opens no TCP connection. It does perform a DNS lookup for a host that has
    already passed every cheaper check, which is why :func:`require_open` runs
    ahead of it: a shut door must not so much as look a name up.
    """
    if not isinstance(url, str) or not url.strip():
        raise _refuse(d, "empty url", "That request had no address to fetch.")
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise _refuse(d, "unparseable url",
                      "That web address could not be read.")

    host = _clean_host(parts.hostname or "")

    if parts.scheme.lower() != "https":
        # Every door here is an https endpoint. Refusing http outright means a
        # downgrade cannot be introduced by a redirect or a hand-edited config.
        raise _refuse(
            d, f"scheme {parts.scheme!r} is not https",
            "That request was refused because it was not an encrypted "
            "connection.", host)
    if parts.username or parts.password:
        # The reason string must not echo the credentials back into the ledger.
        raise _refuse(d, "url carries credentials",
                      "That web address was refused.", host)

    if not host:
        raise _refuse(d, "url has no host", "That web address was refused.")
    if d.dynamic_hosts:
        # Reserved for the Toolsmith, which does not route through here.
        raise _refuse(
            d, "door has dynamic hosts and is not routed through this wrapper",
            "That request must go through the Toolsmith's own fetch path.",
            host)
    if host not in d.hosts:
        raise _refuse(
            d,
            f"host {host!r} is not declared by door {d.id!r} "
            f"({'redirect hop' if hop else 'initial url'}); declared: "
            f"{', '.join(d.hosts)}",
            f"That request tried to reach {host}, which is not one of the "
            f"addresses the '{d.id}' door covers, so nothing was sent.",
            host)

    try:
        port = parts.port
    except ValueError:
        # urlsplit defers port parsing to attribute access, so a URL like
        # ``https://host:notaport/`` only fails here.
        raise _refuse(d, "url has an unreadable port",
                      "That web address was refused.", host)
    if port is not None and port != ALLOWED_PORT:
        raise _refuse(
            d, f"port {port} is not {ALLOWED_PORT}",
            f"That request tried to reach {host} on a port the '{d.id}' door "
            f"does not cover, so nothing was sent.", host)

    # LAST, because it is the only check here that costs a DNS query, and
    # because a host that failed any check above must not be looked up at all —
    # a lookup for an attacker-chosen name is itself a signal leaving the house.
    _check_addresses(d, host, hop=hop)


# ── the ledger: what actually went out, and when ─────────────────────────────

_LAST_USED: dict[str, dict] = {}
_LAST_REFUSED: dict[str, dict] = {}
_ledger_lock = threading.Lock()
_ledger_loaded = False

#: Snapshot format. v1 was ``{door_id: entry}`` with one entry per door, so a
#: refusal followed by a success erased the refusal. v2 keeps both.
_SNAPSHOT_VERSION = 2


def _ledger_path() -> str | None:
    """Where the *snapshot* lives, next to the other security logs.

    The snapshot answers "when did this door last fire, and when was it last
    refused" in O(1) for ``/system/status``. The durable, append-only trail is
    :mod:`puffpai.security.egress_ledger`; this file is its index, not its
    replacement.
    """
    try:
        from puffpai.security.egress_ledger import ledger_dir
        return os.path.join(ledger_dir(), "egress_doors.json")
    except Exception as exc:
        logger.debug("egress door snapshot path unavailable: %s", exc)
        return None


def _load_ledger() -> None:
    """Best effort. A missing or unreadable snapshot just means 'never used'."""
    global _ledger_loaded
    if _ledger_loaded:
        return
    _ledger_loaded = True
    path = _ledger_path()
    if not path:
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(loaded, dict):
        return
    if loaded.get("version") == _SNAPSHOT_VERSION:
        doors = loaded.get("doors")
        if isinstance(doors, dict):
            for key, value in doors.items():
                if key not in _BY_ID or not isinstance(value, dict):
                    continue
                if isinstance(value.get("last"), dict):
                    _LAST_USED[key] = value["last"]
                if isinstance(value.get("last_refused"), dict):
                    _LAST_REFUSED[key] = value["last_refused"]
        return
    # v1: a flat {door_id: entry} map written before the trail existed. Adopt it
    # rather than starting blank, so an upgraded appliance keeps its history.
    for key, value in loaded.items():
        if key in _BY_ID and isinstance(value, dict):
            _LAST_USED[key] = value
            if value.get("result") == "refused":
                _LAST_REFUSED[key] = value


def _write_snapshot() -> None:
    """Persist the last-used / last-refused index. Called with the lock held."""
    path = _ledger_path()
    if not path:
        return
    payload = {
        "version": _SNAPSHOT_VERSION,
        "doors": {
            door_id: {
                "last": _LAST_USED.get(door_id),
                "last_refused": _LAST_REFUSED.get(door_id),
            }
            for door_id in sorted(set(_LAST_USED) | set(_LAST_REFUSED))
        },
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass  # the in-memory record still serves /system/status


def _record(door_id: str, ok: bool, detail: str = "", *,
            host: str = "", path: str = "", bytes_out: int | None = None,
            bytes_in: int | None = None, status: int | None = None,
            kind: str = "request", reason_code: str = "",
            address_class: str = "") -> None:
    """Record one door decision, in the snapshot and in the durable ledger.

    Never raises. Everything written here is content-free by construction: a
    method and a hostname, a URL path with the query already discarded, a
    flag-resolution reason, and *lengths* — never a body, a header or a token.

    ``reason_code`` and ``address_class`` are drawn from closed vocabularies
    (:data:`REASON_RESOLVED_PRIVATE`, :data:`REASON_UNRESOLVABLE`, and
    :data:`puffpai.security.netaddr.ADDRESS_CLASSES`) so that "this door was
    refused because its host pointed indoors" is a value a UI can switch on
    rather than a substring somebody has to grep for. **The resolved address is
    never one of them.** A household's internal addressing is a fact about the
    household, and a security log that quietly records the thing it protects is
    a second copy in a file nobody thinks of as sensitive.

    A refusal also gets exactly one journal line at WARNING, because the owner's
    question after the fact is "did this thing try to phone home", and "there
    is no way to tell" is not an acceptable answer.
    """
    now = datetime.now(timezone.utc)
    entry = {
        "ts": now.isoformat(timespec="seconds"),
        "epoch": round(time.time(), 3),
        "result": "allowed" if ok else "refused",
        "detail": str(detail)[:200],
    }
    if reason_code:
        entry["reason_code"] = str(reason_code)[:64]
    if address_class:
        entry["address_class"] = str(address_class)[:64]
    d = door(door_id)
    caller = "unknown"
    try:
        from puffpai.security import egress_ledger
        caller = egress_ledger.caller_label()
        record = {
            "ts": entry["ts"],
            "door": door_id,
            "result": entry["result"],
            "kind": kind,
            "host": str(host)[:200],
            # Path only — egress_ledger.safe_parts has already dropped the
            # query string, which is where data would hide.
            "path": str(path)[:200],
            "purpose": (d.purpose[:160] if d else ""),
            "third_party": (d.third_party[:160] if d and d.third_party else None),
            "caller": caller,
            "reason": entry["detail"],
            "bytes_out": bytes_out,
            "bytes_in": bytes_in,
            "status": status,
        }
        if reason_code:
            record["reason_code"] = entry["reason_code"]
        if address_class:
            record["address_class"] = entry["address_class"]
        egress_ledger.append(record)
    except Exception as exc:  # a ledger fault must not break a request
        logger.debug("egress ledger append failed for %s: %s", door_id, exc)

    if not ok:
        logger.warning(
            "egress REFUSED: door=%s host=%s caller=%s reason=%s",
            door_id, str(host)[:120] or "-", caller, entry["detail"])
    else:
        logger.debug("egress allowed: door=%s host=%s", door_id, host)

    with _ledger_lock:
        _load_ledger()
        _LAST_USED[door_id] = entry
        if not ok:
            _LAST_REFUSED[door_id] = entry
        _write_snapshot()


def record_external(door_id: str, ok: bool, *, detail: str = "",
                    host: str = "", path: str = "",
                    bytes_out: int | None = None, bytes_in: int | None = None,
                    status: int | None = None, kind: str = "request",
                    reason_code: str = "", address_class: str = "") -> None:
    """Record one decision made by an EXTERNALLY-enforced door's call site.

    :func:`egress_request` records its own calls. A door with
    ``enforcement="external"`` opens its sockets somewhere this module cannot
    wrap — the Toolsmith's hardened fetch loop, the ddgs library — so without
    this it would pass ``require_open`` and then leave no trace of what it
    actually did. A refusal that is logged and an allowed request that is not
    is a ledger that only ever shows the appliance behaving.

    Same content-free contract as every other line written here: a hostname, a
    URL path with the query already discarded, a status code and *lengths* —
    never a body, a header, a query string or a token. Never raises.
    """
    _record(door_id, ok=ok, detail=detail, host=host, path=path,
            bytes_out=bytes_out, bytes_in=bytes_in, status=status, kind=kind,
            reason_code=reason_code, address_class=address_class)


def last_used(door_id: str) -> dict | None:
    """The most recent decision on this door — allowed or refused — if any."""
    with _ledger_lock:
        _load_ledger()
        entry = _LAST_USED.get(door_id)
        return dict(entry) if entry else None


def last_refused(door_id: str) -> dict | None:
    """The most recent *refusal* on this door, if any.

    Separate from :func:`last_used` on purpose: a door that was refused at 02:00
    and allowed at 02:05 must not look as though it was never refused.
    """
    with _ledger_lock:
        _load_ledger()
        entry = _LAST_REFUSED.get(door_id)
        return dict(entry) if entry else None


def door_states() -> dict:
    """:func:`door_status`, wrapped in the object the wizard's Secure card reads.

    One implementation - this calls :func:`door_status` rather than walking the
    registry a second time. The wrapper exists because the card packages came
    from a build whose door table answered ``{"doors": [...]}``  and a second
    walk of the same registry is a second place for the two answers to
    disagree about what is open.
    """
    return {"doors": door_status()}


def door_status() -> list[dict]:
    """Every door, its current state and when it was last used.

    This is what ``/system/status`` publishes, and what the owner reads to
    answer "what can leave my house". It lists doors that this registry does
    not enforce (``enforced: false``) rather than omitting them.
    """
    out: list[dict] = []
    for d in all_doors():
        allowed, reason = egress_allowed(d.id)
        if d.dynamic_hosts:
            hosts: list = ["<per request>"]
        elif d.derived:
            hosts = [f"{h} (via door {src})" for h, _p, src in probe_targets()]
        else:
            hosts = list(d.hosts)
        row = {
            "id": d.id,
            "open": bool(allowed),
            "reason": reason,
            # WHERE this door's current value comes from. A shut door the owner
            # can open and a shut door the unit file forces shut look identical
            # without it, and the second one makes a UI switch a lie.
            "source": flag_source(d),
            "writable": ((d.enforcement in ("registry",)
                          or d.id in _FLAG_HONOURED_EXTERNAL)
                         and not d.derived and not _is_inert(d)),
            # Which app declared this door, or None for a built-in one. The
            # owner's question about an app door is "which app wants this",
            # and an id that merely embeds the answer makes every surface
            # parse a string to find it.
            "app": app_of(d),
            # An externally-enforced door whose call site really does ask this
            # registry IS enforced by this switch, and saying otherwise
            # understates the control the owner has. The distinction that
            # survives is ``enforcement``: WHERE the refusal happens.
            "enforced": (d.enforcement in ("registry", "derived")
                         or d.id in _FLAG_HONOURED_EXTERNAL),
            "enforcement": d.enforcement,
            "enforcement_note": ENFORCEMENT_NOTES.get(d.enforcement, ""),
            "hosts": hosts,
            "purpose": d.purpose,
            "data_sent": d.data_sent,
            "third_party": d.third_party,
            "consent_kind": d.consent_kind,
            "config_flag": f"{CONFIG_SECTION}.{d.config_flag}",
            "env_var": d.env_var,
            "default": d.default,
            "last_used": last_used(d.id),
            "last_refused": last_refused(d.id),
        }
        out.append(row)
    return out


__all__ = [
    "APP_DOOR_PREFIX",
    "CONFIG_SECTION",
    "DEFAULT_TIMEOUT",
    "DOORS",
    "ENFORCEMENT_NOTES",
    "FLAG_SOURCES",
    "MAX_APP_DOORS_PER_APP",
    "MAX_APP_DOOR_HOSTS",
    "SKILL_DOOR_PREFIX",
    "clear_skill_doors",
    "register_skill_doors",
    "skill_door_flag",
    "skill_door_id",
    "skill_doors",
    "skill_of",
    "Door",
    "EgressBlocked",
    "MAX_REDIRECTS",
    "PROBE_PORT",
    "all_doors",
    "app_door_flag",
    "app_door_id",
    "app_doors",
    "app_egress_get",
    "app_egress_request",
    "app_of",
    "clear_app_doors",
    "register_app_doors",
    "door",
    "door_for_host",
    "door_ids",
    "door_status",
    "overlay_path",
    "set_overlay_override",
    "egress_allowed",
    "flag_source",
    "egress_get",
    "egress_request",
    "last_refused",
    "last_used",
    "probe_targets",
    "record_external",
    "require_open",
    "reset_cache",
    "ALLOWED_PORT",
    "REASON_RESOLVED_PRIVATE",
    "REASON_UNRESOLVABLE",
    "set_resolver_override",
]
