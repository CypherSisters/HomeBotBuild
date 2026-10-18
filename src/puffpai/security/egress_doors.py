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

Owner switches and the Bridge
-----------------------------
The owner has two places to flip a switch: Settings (the overlay, below) and
"What leaves your computer" (:mod:`puffpai.security.outside_switches`, which
the build engine's pages write). Both are the owner's own choice; when they
disagree, OFF wins. The Bridge (:mod:`puffpai.security.bridge_guard`) sits
over every tier: sealed, every door is shut; with only switched-on services
allowed, a line in the config file or the environment may switch a door off
but never on.

The build engine's outside services
-----------------------------------
The doors below the household's own (book catalogs, keyed searches, the
owner's GitHub account, Toolsmith's cloud model, result pictures) are shut by
default like every other. A sign-up key reaches the wire only as a
``credential=`` the security package issued (:func:`_bind_credential`), bound
to exact hosts, on the first hop only. Live-card feed doors and browser-app
web doors are registered at run time in tables of their own, and
:func:`build_probe_get` takes one sample look while building. Each writes one
line to the outside-traffic log (:mod:`puffpai.security.egress_ledger`).

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

from puffpai.logging_config import get_logger
from puffpai.security import netaddr

logger = get_logger(__name__)

#: Config section holding the per-door flags.
CONFIG_SECTION = "egress_doors"

#: The ``run_egress`` posture under which the build engine's own modules treat
#: the bot as online (:func:`run_posture`). The doors here are opened by their
#: switches and closed by the Bridge; this value is read by the engine's
#: modules, not by :func:`egress_allowed`.
OPEN_POSTURE = "enumerated"

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

    def __init__(self, door_id: str, reason: str, user_message: str,
                 code: str = ""):
        super().__init__(f"egress refused [{door_id}]: {reason}")
        self.door_id = door_id
        self.reason = reason
        self.user_message = user_message
        #: One word from ``egress_ledger.REASON_CODES`` ('' = work it out
        #: from ``reason``, see :func:`reason_code`).
        self.code = code


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
    #: Manifest ``posture.egress_switches`` key, when the wizard's table names
    #: this door under a different key. None for a door the table does not
    #: list.
    posture_switch: Optional[str] = None
    #: Hosts this door reaches for PICTURES ONLY (an app's picture host): a
    #: plain GET whose answer is an image (never SVG), size-capped
    #: (:func:`_picture_answer`). Never a full host.
    picture_hosts: tuple[str, ...] = ()


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
        settings_hint="Weather needs the open-meteo/weather.gov switch, which is "
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
        settings_hint="Air quality needs the open-meteo air-quality switch, "
                      "which is off. Turn it on in Settings > Egress.",
    ),
    Door(
        id="news_rss",
        hosts=("feeds.npr.org", "feeds.bbci.co.uk", "news.ycombinator.com",
               "feeds.arstechnica.com", "www.theguardian.com",
               "www.theverge.com"),
        purpose="Headlines for the news widget, from the six feeds in "
                "FEED_DIRECTORY.",
        data_sent="Nothing but the request itself: no query, no household "
                  "data. The publisher learns this house's IP address and that "
                  "it asked for their feed.",
        config_flag="news_rss",
        env_var="PUFFPAI_EGRESS_NEWS_RSS",
        consent_kind="owner_flag",
        enforcement="registry",
        settings_hint="News needs the RSS-feeds switch, which is off. Turn it "
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
        hosts=("nominatim.openstreetmap.org", "geocoding-api.open-meteo.com"),
        purpose="Turn a place name into coordinates for the weather-alert tool, "
                "and find the home town you type once (Open-Meteo's town "
                "lookup) so the weather and daily updates know where home is.",
        data_sent="The place name being looked up, which for this appliance "
                  "is usually where the household lives.",
        config_flag="geocode",
        env_var="PUFFPAI_EGRESS_GEOCODE",
        consent_kind="owner_flag",
        enforcement="registry",
        settings_hint="Looking up coordinates needs the OpenStreetMap geocoding "
                      "switch, which is off. Turn it on in Settings > Egress, or "
                      "set your coordinates directly so nothing has to leave.",
        notes="Kept separate from the weather switch on purpose: weather sends "
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
                      "ipapi.co switch, which is off. Turn it on in "
                      "Settings > Egress, or set your location once with "
                      "'set my location to ...' so nothing has to leave.",
        notes="Stays OFF by default, including after a config migration. "
              "The stored "
              "location covers the family's actual use; this switch exists only "
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
        settings_hint="Finding radio stations needs the radio-browser switch, "
                      "which is off. Turn it on in Settings > Egress. Stations "
                      "already saved still play.",
        notes="radio-browser ONLY. TuneIn has its own switch "
              "('tunein' below), because one "
              "'yes' to a volunteer database is not a 'yes' to a commercial "
              "one. Switching this on sends nothing to TuneIn. The "
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
                  "opml.radiotime.com, a commercial company, not a volunteer "
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
        settings_hint="Looking on TuneIn needs the 'tunein' switch, which is "
                      "off. Turn it on in Settings > Egress. Radio still works "
                      "without it: radio-browser does the searching, and saved "
                      "stations still play.",
        notes="Split out of the radio_browser switch on purpose. "
              "Same capability, finding a station to tune, "
              "but not the same disclosure, so it does not get to share the "
              "same switch; see the geocode switch for the precedent ('different "
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
        data_sent="The search query, word for word: to DuckDuckGo on the chat "
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
        settings_hint="Searching the web needs the 'web_search' switch, "
                      "which is off. Turn it on in Settings > Egress.",
        notes="Enforced at TWO call sites, both of which ask "
              "egress_allowed('web_search') before a socket opens: "
              "assistant_tools.web_search (ddgs opens its own sockets, so "
              "egress_request cannot wrap them; the gate runs before ddgs is "
              "imported) and agents/toolsmith._web_search. The Toolsmith's "
              "first hop is the owner's own SearXNG on loopback, which is not "
              "itself egress, but that container then queries upstream "
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
        settings_hint="Reading a web page needs the 'web_fetch' switch, "
                      "which is off. Turn it on in Settings > Egress.",
        notes="Enforced at the call site in agents/toolsmith.py, which asks "
              "egress_allowed('web_fetch') at the top of _web_fetch and again "
              "at the top of _fetch_url (the one hop loop both fetch paths, "
              "web_fetch and search_and_read, reach the wire through), "
              "BEFORE any URL shape check, DNS resolution or socket. Not "
              "rerouted through egress_request because this switch has no fixed "
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
        purpose="offline_guard.has_internet(): 'is there a WAN link at all?', "
                "asked before an online feature reports itself offline.",
        data_sent="Nothing this switch chooses. The probe opens a TCP "
                  "connection to a host that is ALREADY behind a switch you "
                  "turned on, so it can tell you nothing a switch you turned on "
                  "does not already tell that same host. With every switch off "
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
              "from the switches that are on right now, so a fully shut "
              "appliance opens zero external "
              "sockets. The flag is a hard OFF switch, not an on switch: set "
              "it false and has_internet() stops probing entirely and reports "
              "offline. Leaving it unset does not open anything: the open "
              "switches decide.",
    ),
    Door(
        id="model_pull",
        hosts=("registry.ollama.ai",),
        purpose="Download a language model into Ollama on this box: the "
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
        settings_hint="Downloading a model needs the 'model_pull' switch, "
                      "which is off. Turn it on by setting "
                      "egress_doors.model_pull to true in "
                      "config/puffpai_config.json (or "
                      "PUFFPAI_EGRESS_MODEL_PULL=1 in the environment), or run "
                      "`ollama pull <model>` yourself from a machine that is "
                      "already online.",
        notes="Enforced at the call site, brain_health.pull_model, which asks "
              "egress_allowed('model_pull') before the POST. It cannot go "
              "through egress_request because OUR socket goes to Ollama at "
              "config `ollama.url` (loopback, or the LAN address the owner "
              "configured), and it is OLLAMA that then fetches from "
              "registry.ollama.ai. The box egresses even though this process "
              "does not, which is exactly why the row says registry.ollama.ai "
              "and not the Ollama URL: the switch names where the data actually "
              "goes. The wizard offers the download only with this switch on "
              "and prints the manual command otherwise.",
    ),
    Door(
        id="embedding_model_download",
        hosts=("chroma-onnx-models.s3.amazonaws.com",),
        purpose="Fetch the sentence-embedding model files ChromaDB needs, once "
                "(~80 MB). Without them the knowledge library can be listed "
                "and read but not searched by meaning.",
        data_sent="Nothing but the request itself: no query, no household "
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
                      "'embedding_model_download' switch is off. Turn it on by "
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
              "this switch is never consulted again, and a box that got them by "
              "sneakernet never opens it at all. The same check runs again on "
              "every call of the built function, and before the knowledge "
              "search's degraded fallback (used when the configured embedder "
              "fails) lets chroma embed a question with a collection's own "
              "function: with the files absent and this switch off, that "
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
    # -- the build engine's outside services ---------------------------------
    # Book catalogs: reached only by the book fetcher (source_fetch.get_small
    # under the matching service id) for a book the owner said yes to. Their
    # one switch is the matching row on "What leaves your computer"
    # (outside_switches), read before any socket opens; these rows name the
    # hosts and what is sent.
    Door(
        id="wikipedia_books",
        hosts=("en.wikipedia.org", "simple.wikipedia.org"),
        purpose="Wikipedia articles about this bot's subject, fetched once "
                "and saved on this computer as a book, when you say yes to "
                "that book.",
        data_sent="The book's subject words (for example: knitting, crochet) "
                  "and the names of the articles it reads. Nothing about you "
                  "or your household.",
        config_flag="wikipedia_books",
        env_var="PUFFPAI_EGRESS_WIKIPEDIA_BOOKS",
        consent_kind="owner_flag_third_party",
        enforcement="external",
        third_party="Wikipedia (the Wikimedia Foundation)",
        probe_ok=False,
        settings_hint="Wikipedia is asked only when you say yes to a Wikipedia "
                      "book on the Find more knowledge panel, or with: "
                      "puffpai sources add <book>.",
        notes="Polite by rule: a named User-Agent, maxlag on every request, "
              "at least one second between requests, at most 300 articles "
              "and 25 MB of text per book.",
    ),
    Door(
        id="gutenberg_books",
        hosts=("gutendex.com", "www.gutenberg.org"),
        purpose="Free public-domain books from Project Gutenberg: a search "
                "when you look for books, and the book's text when you say "
                "yes to one.",
        data_sent="The words you searched for (to gutendex.com) and the "
                  "number of the book you chose (to www.gutenberg.org). "
                  "Nothing about you or your household.",
        config_flag="gutenberg_books",
        env_var="PUFFPAI_EGRESS_GUTENBERG_BOOKS",
        consent_kind="owner_flag_third_party",
        enforcement="external",
        third_party="Project Gutenberg and Gutendex",
        probe_ok=False,
        settings_hint="Project Gutenberg is asked only when you search for "
                      "books or say yes to one on the Find more knowledge "
                      "panel, or with: puffpai sources add <book>.",
        notes="At most 25 MB per book; Gutenberg's own plain-text file only.",
    ),
    Door(
        id="openstax_books",
        hosts=("openstax.org",),
        purpose="Free OpenStax textbooks: their list when you look for books, "
                "and a textbook's pages when you say yes to one.",
        data_sent="Nothing but the requests themselves: which book list and "
                  "which textbook pages are read. Nothing about you or your "
                  "household.",
        config_flag="openstax_books",
        env_var="PUFFPAI_EGRESS_OPENSTAX_BOOKS",
        consent_kind="owner_flag_third_party",
        enforcement="external",
        third_party="OpenStax (Rice University)",
        probe_ok=False,
        settings_hint="OpenStax is asked only when you search for books or say "
                      "yes to a textbook on the Find more knowledge panel, or "
                      "with: puffpai sources add <book>.",
        notes="At most 600 pages and 25 MB of text per textbook, one page a "
              "second.",
    ),
    Door(
        id="wikibooks_books",
        hosts=("en.wikibooks.org",),
        purpose="Free how-to books from Wikibooks: a search when you look for "
                "books, and a book's pages when you say yes to one.",
        data_sent="The words you searched for and the names of the pages of "
                  "the book you chose. Nothing about you or your household.",
        config_flag="wikibooks_books",
        env_var="PUFFPAI_EGRESS_WIKIBOOKS_BOOKS",
        consent_kind="owner_flag_third_party",
        enforcement="external",
        third_party="Wikibooks (the Wikimedia Foundation)",
        probe_ok=False,
        settings_hint="Wikibooks is asked only when you search for books or say "
                      "yes to one, or with: puffpai sources add <book>.",
        notes="At most 300 pages and 25 MB of text per book, one page a second.",
    ),
    Door(
        id="archive_books",
        hosts=("archive.org",),
        purpose="Old public-domain books scanned by libraries, from the "
                "Internet Archive: a search when you look for books, and the "
                "book's text when you say yes to one.",
        data_sent="The words you searched for and the id of the book you "
                  "chose. Nothing about you or your household.",
        config_flag="archive_books",
        env_var="PUFFPAI_EGRESS_ARCHIVE_BOOKS",
        consent_kind="owner_flag_third_party",
        enforcement="external",
        third_party="The Internet Archive",
        probe_ok=False,
        settings_hint="The Internet Archive is asked only when you search for "
                      "books or say yes to one, or with: puffpai sources add "
                      "<book>.",
        notes="Only books marked not in copyright and published long enough "
              "ago; the Archive's own text of the scan, at most 25 MB.",
    ),
    # Keyed searches: a sign-up key the owner added on the Sign-up keys page
    # rides only as a bound credential (security/keys.py), only to this host.
    Door(
        id="brave_search",
        hosts=("api.search.brave.com",),
        purpose="Searching the whole web with Brave when you ask me to "
                "search. Needs your own free key from Brave.",
        data_sent="The words you asked me to search for, after private "
                  "details are taken out, your key from Brave, and your "
                  "internet address.",
        config_flag="brave_search",
        env_var="PUFFPAI_EGRESS_BRAVE_SEARCH",
        consent_kind="owner_flag_third_party",
        enforcement="registry",
        third_party="Brave Software",
        probe_ok=False,
        settings_hint="Searching with Brave is switched off, so I did not "
                      "search with it. Adding your Brave key on the Sign-up "
                      "keys page switches it on.",
        notes="Keyed (security/keys.py): the key rides in a header, only to "
              "this host, never while the vault is locked (Brave asks for a "
              "payment card). Stops at the local monthly cap.",
    ),
    Door(
        id="marginalia_search",
        hosts=("api2.marginalia-search.com",),
        purpose="Searching the web with Marginalia when you ask me to "
                "search. Needs your own free key; free for personal use only.",
        data_sent="The words you asked me to search for, after private "
                  "details are taken out, your key from Marginalia, and your "
                  "internet address.",
        config_flag="marginalia_search",
        env_var="PUFFPAI_EGRESS_MARGINALIA_SEARCH",
        consent_kind="owner_flag_third_party",
        enforcement="registry",
        third_party="Marginalia Search",
        probe_ok=False,
        settings_hint="Searching with Marginalia is switched off, so I did "
                      "not search with it. Adding your Marginalia key on the "
                      "Sign-up keys page switches it on.",
        notes="Keyed (security/keys.py): header only, only to this host.",
    ),
    # The owner's own GitHub account: reached only by puffpai.github_account
    # and the GitHub searcher, after the owner connects the account (that tap
    # is the owner's yes to this switch).
    Door(
        id="github",
        hosts=("api.github.com", "github.com"),
        purpose="Your own GitHub account, when you connect it: better searches, "
                "your own projects and the ones you starred as finds, and a "
                "private project of yours when you say yes to it.",
        data_sent="Your GitHub token (only to api.github.com, never in an "
                  "address), the short search words shown on screen, the names "
                  "of your projects, and your internet address. Connecting "
                  "without a token sends a one-time sign-in code request to "
                  "github.com.",
        config_flag="github",
        env_var="PUFFPAI_EGRESS_GITHUB",
        consent_kind="owner_flag_third_party",
        enforcement="registry",
        third_party="GitHub",
        probe_ok=False,
        settings_hint="Your GitHub account is switched off, so nothing was sent "
                      "to GitHub with it. Connecting it again on the Developer "
                      "page switches it on.",
        notes="Keyed (security/keys.py, row 'github', vault only): the token "
              "rides only in a header to api.github.com, only while the vault "
              "is open, never in an address, never on a redirect. github.com "
              "is reached only for the device sign-in, which carries no token.",
    ),
    # Toolsmith's cloud model. Toolsmith is the only agent that may use a
    # cloud model; it stays on the model on this computer unless the owner
    # chooses the cloud one in Settings.
    Door(
        id="toolsmith_cloud",
        hosts=("api.anthropic.com",),
        purpose="Toolsmith's cloud model, when you choose Anthropic for it in "
                "Settings: it works out how to install and start an app.",
        data_sent="When Toolsmith finds and installs apps: your goal and your "
                  "bot's jobs, the names and descriptions of the apps it found, "
                  "each app's README, docs and file list, the start of its setup "
                  "files, the commands it runs with their output, and what the "
                  "app's pages show (a folder of your own is read the same way), "
                  "after keys and private details are taken out, with your "
                  "Anthropic key (only to api.anthropic.com) and your internet "
                  "address. Never your chats or vault.",
        config_flag="toolsmith_cloud",
        env_var="PUFFPAI_EGRESS_TOOLSMITH_CLOUD",
        consent_kind="owner_flag_third_party",
        enforcement="registry",
        third_party="Anthropic",
        probe_ok=False,
        settings_hint="Toolsmith's cloud model is switched off, so nothing was "
                      "sent to Anthropic. Toolsmith uses the model on this "
                      "computer.",
        notes="Keyed (security/keys.py, row 'anthropic', vault only): the key "
              "rides only in a header to api.anthropic.com, only while the vault "
              "is open, never on a redirect. Called only from Toolsmith's "
              "install loop (security/toolsmith_cloud.py scope guard).",
    ),
    Door(
        id="result_pictures",
        hosts=(),
        dynamic_hosts=True,
        purpose="Pictures a data source's own results point to (a news story's "
                "photo, a library's thumbnail), shown in an app your bot made.",
        data_sent="One picture request to the address the source's own answer "
                  "named. That site learns your internet address and which "
                  "picture was asked for. Nothing you typed, no cookie, no key.",
        config_flag="result_pictures",
        env_var="PUFFPAI_EGRESS_RESULT_PICTURES",
        consent_kind="derived_from_open_doors",
        enforcement="derived",
        probe_ok=False,
        settings_hint="Pictures from a source's results are switched off on this "
                      "computer, so none was fetched.",
        notes="Never opened by a switch of its own: only through "
              ":func:`result_picture_get`, which needs the SOURCE's own switch "
              "on (your yes to \"<source> and the picture sites its results "
              "point to\") and a host that appeared in that source's results. "
              "GET only, image content types only (no SVG), size-capped, one "
              "audit line per host. Its env var may switch it off, never on.",
    ),
)

#: The picture door: opened only by :func:`result_picture_get`.
RESULT_PICTURES = "result_pictures"
#: The largest picture a source's result may point to.
RESULT_PICTURE_MAX_BYTES = 8 * 1024 * 1024
#: Picture content types let through (never SVG: it can carry script).
RESULT_PICTURE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp", "image/avif")

#: Doors a sign-up key rides on. A caller-supplied ``auth=``, ``cookies=`` or
#: credential-shaped header is refused on them: the key goes only as a bound
#: ``credential=`` from the security package.
KEYED_DOORS = frozenset({"github", "toolsmith_cloud", "brave_search",
                         "marginalia_search"})

_BY_ID: dict[str, Door] = {d.id: d for d in DOORS}
_HOST_INDEX: dict[str, Door] = {
    host: d for d in DOORS for host in d.hosts
}


# ── config + flag resolution ─────────────────────────────────────────────────

_lock = threading.Lock()
_cache: dict | None = None
#: The stamp of the config file :data:`_cache` was read from.
_cache_stamp: Any = None

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
    """The whole config file, cached until the file changes. Never raises.

    Read directly rather than through the config dataclass because unknown keys
    are dropped there, and a door added by a build must resolve without the
    loader having been taught about it. The file's stamp is checked on every
    call, so a switch written by another process is in force at once.
    """
    global _cache, _cache_stamp
    path = _config_path()
    stamp = _file_stamp(path) if path else _NO_STAMP
    with _lock:
        if (_cache is not None and stamp is not _NO_STAMP
                and stamp == _cache_stamp):
            return _cache
        raw: dict = {}
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
        _cache_stamp = stamp
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
    """Forget the cached config, overlay and posture (tests, and a reload after
    an edit)."""
    global _cache, _cache_stamp, _overlay_cache, _posture_cache
    with _lock:
        _cache = None
        _cache_stamp = None
        _overlay_cache = _OVERLAY_UNSET
        _posture_cache = None
    _reset_app_consent_cache()


#: The run posture read by :func:`run_posture`, with the stamp of the build
#: manifest it came from, so a change written by another process is seen on
#: the next call.
_posture_cache: Optional[dict] = None
#: A stamp that never matches: the file could not be looked at, so it is read
#: again (and fails closed) every time instead of being cached.
_NO_STAMP = object()


def _file_stamp(path: str) -> Any:
    """``(path, mtime_ns, size, inode)`` of *path*; ``(path, None, None, None)``
    when it does not exist; :data:`_NO_STAMP` when it cannot be looked at."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return (path, None, None, None)
    except OSError:
        return _NO_STAMP
    return (path, st.st_mtime_ns, st.st_size, getattr(st, "st_ino", 0))


def _manifest_stamp() -> Any:
    try:
        from puffpai import build_manifest
        return _file_stamp(build_manifest.manifest_path())
    except Exception:
        return _NO_STAMP


def run_posture() -> dict[str, Any]:
    """This build's run-time egress posture, normalised and fail-closed.

    Reads ``posture`` from the build manifest and puts it through
    :func:`puffpai.build_posture.assess_posture`, which is where the
    normalisation already lives: an unrecognised ``run_egress`` becomes the
    safe default rather than being honoured, and switches only mean anything
    under ``enumerated``. That matters because :func:`build_manifest.read_manifest`
    returns the file's contents unprojected, so a hand-edited manifest could
    otherwise put an arbitrary string where a posture belongs.

    No manifest at all means no wizard has run, which resolves to the same safe
    default (``island``) and therefore to every door shut.

    Cached until the manifest file changes (its stamp is checked on every
    call), so a posture written by another process is in force at once.
    """
    global _posture_cache
    stamp = _manifest_stamp()
    with _lock:
        if (_posture_cache is not None and stamp is not _NO_STAMP
                and _posture_cache.get("stamp") == stamp):
            return _posture_cache["value"]
    from puffpai import build_posture
    block: dict[str, Any] = {}
    try:
        from puffpai import build_manifest
        data = build_manifest.read_manifest()
        if isinstance(data, dict) and isinstance(data.get("posture"), dict):
            block = data["posture"]
    except Exception as exc:
        logger.debug("build manifest unavailable for egress posture: %s", exc)
    switches = block.get("egress_switches")
    enabled = [k for k, v in switches.items() if v is True]         if isinstance(switches, dict) else None
    settled = build_posture.assess_posture(
        block.get("build_network"), block.get("run_egress"), enabled)
    out = {
        "run_egress": settled["run_egress"],
        "source": settled["run_egress_source"],
        "switches": dict(settled["egress_switches"]),
    }
    with _lock:
        _posture_cache = {"stamp": stamp, "value": out}
    return out


def _shown(name: Any) -> str:
    """An id someone else chose (an app's, a door's) as a log line may show
    it: plain and key-safe, else "(not shown)". Fail closed."""
    try:
        from puffpai import web_search
        return web_search.log_name(name, placeholder="(not shown)", limit=64)
    except Exception:
        return "(not shown)"


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


def app_door_id(app_id: str, index: "int | None" = None) -> str:
    """The registry id for one manifest door, ``app:<app_id>:<n>``, or with no
    *index* for the app's live feed door, ``app:<app_id>:feed``."""
    if index is None:
        return f"{APP_DOOR_PREFIX}{app_id}:feed"
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


def app_door_flag(app_id: str, index, hosts=None) -> str:
    """The config/overlay key for one manifest door, ``(app_id, index, hosts)``.

    Called as ``(app_id, hosts)`` it is the key for the app's live feed door.
    Either way it is bound to the door's host set.
    """
    if hosts is None:
        clean = tuple(sorted({_clean_host(h) for h in (index or ())
                              if _clean_host(h)}))
        return f"app_{app_id}_feed_{_app_host_digest(clean)}"
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
            (f"The '{app_id}' app's switch is off. Turn it on in "
             f"Settings > Egress if you want this appliance talking to "
             f"{', '.join(hosts)}.") if hosts else
            (f"The '{app_id}' app's switch is off and names no host: the app "
             f"declares that it reaches nowhere, so there is nothing to turn "
             f"on.")),
        # NEVER from the manifest. Stated as a literal so that the invariant is
        # a value the tests assert on rather than a promise in a comment.
        default=False,
        notes=(f"Declared by the '{app_id}' app manifest and registered on "
               f"rescan. Shut until the owner opens it; a host this door does "
               f"not name is refused even when it is open."),
    )


def _register_manifest_doors(entries) -> tuple[str, ...]:
    """Replace the whole manifest app-door set from *entries*: ``[(app_id, doors), ...]``.

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
    """Forget every registered app door, manifest and feed (a build with no
    apps, and tests)."""
    with _APP_LOCK:
        _APP_DOORS.clear()
        _FEED_DOORS.clear()


def app_doors() -> tuple[Door, ...]:
    """The registered app doors, manifest then feed, in registration order."""
    with _APP_LOCK:
        return tuple(_APP_DOORS.values()) + tuple(_FEED_DOORS.values())


# ── live-card feed doors: an app's declared feed host, as a real door ────────
#
# An app's ``feed`` block (:mod:`puffpai.feeds`) names ONE host its data comes
# from, and optionally one host its pictures come from. These functions turn
# that declaration into an ordinary :class:`Door` (``app:<app_id>:feed``), so
# the feed runner goes through :func:`egress_request` like every other
# outbound call. They live in their own table beside the manifest doors above,
# for the same reason skill doors do: each registration is a wholesale
# replace, and one subsystem's rescan must not delete the other's doors.
#
# 1. **Forced shut.** ``default`` is the literal False below and is never read
#    from a manifest. The ways one opens are the owner's recorded yes
#    (:func:`set_app_door_consent`, reached from ``puffpai apps allow`` and
#    ``POST /apps/<id>/feed/allow``) or the owner's Settings switch; a line in
#    the config file only while every service may be used as built.
# 2. **Consent is bound to the hosts it was given for.** The flag carries an
#    8-hex digest of the sorted host set, so an app update that repoints its
#    feed falls back to shut.
# 3. **The owner's yes lives in the state dir**: ``<state>/egress_app_doors.json``,
#    re-read when its mtime changes. An unreadable or malformed file is no
#    setting at all: every feed door shut.
# 4. **Operator kill switch.** ``PUFFPAI_EGRESS_APPS=off`` forces every feed
#    and browser-app door shut. Only "off" is honoured there.

#: Operator override that forces every feed and browser-app door shut.
APPS_KILL_ENV = "PUFFPAI_EGRESS_APPS"

#: File name of the owner's recorded yes/no, under the state dir.
APP_CONSENT_FILE = "egress_app_doors.json"
APP_CONSENT_SCHEMA = "puffpai_app_door_consent/v1"

#: A live-card app's id: the manifest id rule with a tighter length bound.
_FEED_APP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

#: Suffixes that only ever mean "this network", never a public service.
_LOCAL_TLDS = frozenset({"local", "lan", "internal", "localhost", "home",
                         "arpa", "localdomain", "intranet", "corp"})

#: At most this many hosts per feed or browser-app door: the data host and
#: the picture host.
MAX_FEED_DOOR_HOSTS = 2
#: At most this many outside sites per browser app (the engine's web apps):
#: the same cap as a feed door.
MAX_WEB_APP_HOSTS = MAX_FEED_DOOR_HOSTS

#: The consent binding for a feed or browser-app door's host set (the same
#: digest the manifest doors use).
_host_digest = _app_host_digest

#: The live-card feed doors, under :data:`_APP_LOCK`.
_FEED_DOORS: dict[str, Door] = {}

_consent_lock = threading.Lock()
_consent_cache: dict[str, Any] = {"path": None, "mtime": None, "doors": {}}


def is_ip_literal(host: Any) -> bool:
    """True if *host* is an address rather than a name, in ANY notation."""
    return _is_ip_literal(host)


def is_registrable_hostname(host: Any) -> bool:
    """True only for a public NAME a door may declare. Refuses every literal.

    Lower-case hostname shape, at least two labels, a TLD-shaped last label
    that is not a local-network suffix, and not an address in any notation.
    ``localhost``, ``nas.local``, ``8.8.8.8``, ``127.1``, ``0x7f000001`` and
    ``[::1]`` all fail.
    """
    if not isinstance(host, str) or host != host.strip().lower():
        return False
    if len(host) > 253 or not _APP_HOST_RE.match(host):
        return False
    if _is_ip_literal(host):
        return False
    labels = host.split(".")
    if len(labels) < 2:
        return False
    if labels[-1] in _LOCAL_TLDS:
        return False
    return bool(_APP_TLD_RE.match(labels[-1]))


def is_app_door(d: "Door | str | None") -> bool:
    """True for a door an app declared (manifest, feed or browser app)."""
    key = d.id if isinstance(d, Door) else str(d or "")
    return key.startswith(APP_DOOR_PREFIX)


def _is_feed_door(d: "Door | str | None") -> bool:
    """True for a registered live-card feed door."""
    key = d.id if isinstance(d, Door) else str(d or "")
    if not (key.startswith(APP_DOOR_PREFIX) and key.endswith(":feed")):
        return False
    with _APP_LOCK:
        return key in _FEED_DOORS


def _feed_door_from_spec(app_id: str, spec: Any) -> Optional[Door]:
    """One ``Door`` from one feed spec, or None, never a half-trusted door.

    *spec* is ``{"hosts": [...], "picture_hosts": [...], "name": str,
    "purpose": str, "data_sent": str}``. Every host is re-checked here even
    though the loader already did.
    """
    if not isinstance(spec, Mapping):
        return None
    raw_hosts = spec.get("hosts")
    if not isinstance(raw_hosts, (list, tuple)) or not raw_hosts:
        return None
    hosts: list[str] = []
    for raw in raw_hosts:
        if not isinstance(raw, str) or not is_registrable_hostname(raw):
            return None
        if raw not in hosts:
            hosts.append(raw)
    pictures: list[str] = []
    raw_pics = spec.get("picture_hosts") or []
    if not isinstance(raw_pics, (list, tuple)):
        return None
    for raw in raw_pics:
        if not isinstance(raw, str) or not is_registrable_hostname(raw):
            return None
        if raw not in hosts and raw not in pictures:
            pictures.append(raw)
    if len(hosts) + len(pictures) > MAX_FEED_DOOR_HOSTS:
        return None
    name = spec.get("name")
    name = str(name)[:64] if isinstance(name, str) and name.strip() else app_id
    purpose = spec.get("purpose")
    data_sent = spec.get("data_sent")
    # Bound to every site it reaches, pictures-only ones too.
    flag = app_door_flag(app_id, hosts + pictures)
    return Door(
        id=app_door_id(app_id),
        hosts=tuple(hosts),
        picture_hosts=tuple(pictures),
        purpose=(str(purpose)[:200] if isinstance(purpose, str) else ""),
        data_sent=(str(data_sent)[:200] if isinstance(data_sent, str) else ""),
        config_flag=flag,
        env_var="PUFFPAI_EGRESS_" + flag.upper(),
        consent_kind="owner_flag",
        enforcement="registry",
        probe_ok=False,
        settings_hint=(
            f"{name} has not been allowed to use {', '.join(hosts)} yet, so "
            f"nothing was fetched. To allow it, run: puffpai apps allow "
            f"{app_id}"),
        # NEVER from the manifest. A literal, so a test can assert on it.
        default=False,
        notes=(f"Declared by the '{app_id}' app's feed block and registered on "
               f"scan. Shut until the owner allows it; a host this door does "
               f"not name is refused even when it is open."),
    )


def register_feed_doors(entries) -> tuple[str, ...]:
    """Replace the whole feed-door set from *entries*: ``[(app_id, spec), ...]``.

    A wholesale replace, not a merge: a rescan IS install and uninstall, so an
    app whose folder is gone loses its door in the same breath. Never raises; a
    refused entry simply has no door, which is the fail-shut outcome. The
    manifest doors and skill doors are untouched.
    """
    built: dict[str, Door] = {}
    for entry in (entries or ()):
        try:
            app_id, spec = entry
        except (TypeError, ValueError):
            continue
        app_id = str(app_id or "").strip()
        if not _FEED_APP_ID_RE.match(app_id):
            logger.warning("egress: refusing an app feed door for unusable "
                           "app id %s", _shown(app_id))
            continue
        d = _feed_door_from_spec(app_id, spec)
        if d is None:
            logger.warning("egress: app %s feed door is not registrable; the "
                           "app reaches nothing", _shown(app_id))
            continue
        if d.id in _BY_ID or d.id in built:
            logger.warning("egress: refusing duplicate app door id %s",
                           _shown(d.id))
            continue
        built[d.id] = d
    with _APP_LOCK:
        changed = set(built) != set(_FEED_DOORS) or any(
            _FEED_DOORS.get(k) != v for k, v in built.items())
        _FEED_DOORS.clear()
        _FEED_DOORS.update(built)
    if changed:
        logger.info("egress: %d app feed door(s) registered, all shut until "
                    "the owner allows them: %s", len(built),
                    ", ".join(_shown(k) for k in built) or "-")
    return tuple(built)


def _is_feed_entry(entry: Any) -> bool:
    """True for a ``(app_id, {feed spec})`` registration entry."""
    return (isinstance(entry, (tuple, list)) and len(entry) == 2
            and isinstance(entry[1], Mapping))


def register_app_doors(entries) -> tuple[str, ...]:
    """Replace an app-door table from *entries*. Returns the ids registered.

    Two kinds of entry share this name, each with its own table:
    ``(app_id, [door block, ...])`` from an app manifest's ``doors[]``, and
    ``(app_id, {"hosts": [...], ...})`` from an app's live feed
    (:func:`register_feed_doors`). Each call replaces only the table its
    entries belong to. An empty call from the feed runner (``puffpai.feeds``)
    clears the feed table; any other empty call clears the manifest table.
    Never raises.
    """
    items = list(entries or ())
    feed = [e for e in items if _is_feed_entry(e)]
    manifest = [e for e in items if not _is_feed_entry(e)]
    if not items:
        caller = sys._getframe(1).f_globals.get("__name__", "")
        if caller == "puffpai.feeds":
            return register_feed_doors([])
        return _register_manifest_doors([])
    out: tuple[str, ...] = ()
    if manifest:
        out += _register_manifest_doors(manifest)
    if feed:
        out += register_feed_doors(feed)
    return out


def app_door(app_id: str) -> Optional[Door]:
    """The registered feed door for *app_id*, or None."""
    with _APP_LOCK:
        return _FEED_DOORS.get(app_door_id(str(app_id or "").strip()))


def feed_doors() -> tuple[Door, ...]:
    """The registered live-card feed doors, in registration order."""
    with _APP_LOCK:
        return tuple(_FEED_DOORS.values())


# -- the owner's recorded yes/no for a feed door --------------------------------

def app_consent_path() -> str:
    """``<state dir>/egress_app_doors.json``. Never inside the checkout."""
    from puffpai.config import get_state_dir
    return os.path.join(get_state_dir(), APP_CONSENT_FILE)


def _reset_app_consent_cache() -> None:
    with _consent_lock:
        _consent_cache.update({"path": None, "mtime": None, "doors": {}})


def _read_app_consent() -> dict[str, Any]:
    """``{flag: {"open": bool, ...}}`` from the state file. Fails to {}.

    Re-read when the path or mtime changes. A missing file is no setting; an
    unreadable, oversize or malformed one is ALSO no setting, and is logged:
    either way every feed door stays shut.
    """
    try:
        path = app_consent_path()
    except Exception as exc:
        logger.debug("app door consent path unavailable: %s", exc)
        return {}
    try:
        stat = os.stat(path)
        mtime = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        with _consent_lock:
            _consent_cache.update({"path": path, "mtime": None, "doors": {}})
        return {}
    with _consent_lock:
        if _consent_cache["path"] == path and _consent_cache["mtime"] == mtime:
            return dict(_consent_cache["doors"])
    doors: dict[str, Any] = {}
    try:
        if stat.st_size > 256 * 1024:
            raise ValueError("consent file is larger than 256 KB")
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if (isinstance(loaded, dict)
                and loaded.get("schema") == APP_CONSENT_SCHEMA
                and isinstance(loaded.get("doors"), dict)):
            for flag, entry in loaded["doors"].items():
                if (isinstance(flag, str) and isinstance(entry, dict)
                        and isinstance(entry.get("open"), bool)):
                    doors[flag] = dict(entry)
        else:
            logger.warning("app door consent file %s is not in the expected "
                           "shape; every app door stays shut", path)
    except (OSError, ValueError) as exc:
        logger.warning("app door consent file unreadable (%s); every app door "
                       "stays shut", exc)
        doors = {}
    with _consent_lock:
        _consent_cache.update({"path": path, "mtime": mtime, "doors": doors})
    return dict(doors)


def _app_door_switch(d: Door) -> tuple[Optional[bool], str]:
    """The owner's setting for a feed door, and where it came from.

    Order: the operator kill switch (off only), the per-door env var (with
    only switched-on services allowed it may switch off, never on), the
    owner's recorded yes/no and the owner's Settings switch (OFF wins), then
    ``egress_doors.<flag>`` in config while every service may be used as
    built, else none.
    """
    kill = os.environ.get(APPS_KILL_ENV)
    if kill is not None and _as_bool(kill) is False:
        return False, f"{APPS_KILL_ENV}=off forces every app door shut"

    bridge_up = _bridge_up()
    raw_env = os.environ.get(d.env_var)
    if raw_env is not None and raw_env.strip() != "":
        decided = _as_bool(raw_env)
        if decided is None:
            return None, f"{d.env_var} is not a switch value; door stays shut"
        if decided is False or not bridge_up:
            return decided, f"{d.env_var}={'on' if decided else 'off'}"

    entry = _read_app_consent().get(d.config_flag)
    recorded = (entry["open"] if isinstance(entry, dict)
                and isinstance(entry.get("open"), bool) else None)
    overlay = _raw_overlay()
    if overlay is None:
        return False, "the owner overlay will not parse; every door stays shut"
    chosen = _as_bool(overlay.get(d.config_flag)) if d.config_flag in overlay else None
    if recorded is False or chosen is False:
        return False, "the owner said no"
    if recorded is True or chosen is True:
        return True, "the owner allowed it"

    if bridge_up:
        # With only switched-on services allowed, a live card opens only on
        # the owner's own yes, never on a line in a settings file.
        return None, "the Bridge is up and the owner has not allowed it"

    section = _raw_config().get(CONFIG_SECTION)
    if isinstance(section, dict) and d.config_flag in section:
        decided = _as_bool(section.get(d.config_flag))
        if decided is None:
            return None, (f"{CONFIG_SECTION}.{d.config_flag} is not a switch "
                          f"value; door stays shut")
        return decided, f"{CONFIG_SECTION}.{d.config_flag}={decided}"

    return None, "the owner has not allowed it"


def _feed_door_flag(d: Door, level: str) -> tuple[bool | None, str, str]:
    """:func:`_resolve_flag` for a feed door: ``(value, source, reason)``."""
    del level  # the Bridge is read inside _app_door_switch
    decided, why = _app_door_switch(d)
    if why.startswith(APPS_KILL_ENV) or why.startswith(d.env_var):
        source = "env"
    elif why.startswith(CONFIG_SECTION):
        source = "config"
    elif "Bridge" in why:
        source = "bridge"
    elif decided is None:
        source = "default"
    else:
        source = "owner_switch"
    return decided, source, why


def app_door_consent(app_id: str) -> Optional[bool]:
    """What the owner recorded for this app's feed door: True, False or None.

    Only the recorded yes/no (and config), not the Bridge: "did the owner say
    yes" and "is the door open" are different questions and a screen needs
    both answers.
    """
    d = app_door(app_id)
    if d is None:
        return None
    decided, _why = _app_door_switch(d)
    return decided


def set_app_door_consent(app_id: str, allowed: bool, *,
                         when: Optional[str] = None) -> dict[str, Any]:
    """Record the owner's yes or no for an app's feed door. Opens no socket.

    Refuses an app with no registered feed door. The write is atomic (temp
    file + replace) and keyed by the host-bound flag, so a yes never carries
    over to a different host set. Returns ``{"door", "flag", "hosts", "open"}``.
    """
    if not isinstance(allowed, bool):
        raise ValueError("allowed must be True or False")
    d = app_door(app_id)
    if d is None:
        raise KeyError(f"no feed door is registered for app {app_id!r}")
    path = app_consent_path()
    stamp = when or datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _consent_lock:
        existing: dict[str, Any] = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if (isinstance(loaded, dict)
                    and loaded.get("schema") == APP_CONSENT_SCHEMA
                    and isinstance(loaded.get("doors"), dict)):
                existing = {k: v for k, v in loaded["doors"].items()
                            if isinstance(k, str) and isinstance(v, dict)}
        except (OSError, ValueError):
            existing = {}
        existing[d.config_flag] = {
            "open": allowed, "app": app_of(d),
            "hosts": list(d.hosts) + list(d.picture_hosts),
            "at": stamp,
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"schema": APP_CONSENT_SCHEMA, "doors": existing}, fh,
                      indent=2, sort_keys=True)
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover - Windows ignores the mode
            pass
        os.replace(tmp, path)
        _consent_cache.update({"path": None, "mtime": None, "doors": {}})
    logger.info("egress: owner %s app door %s (%s)",
                "allowed" if allowed else "shut", _shown(d.id),
                ", ".join(_shown(h) for h in d.hosts))
    return {"door": d.id, "flag": d.config_flag, "hosts": list(d.hosts),
            "open": allowed}


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
            f"The '{skill_id}' skill's switch is off. Turn it on in "
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
        found = _APP_DOORS.get(key) or _FEED_DOORS.get(key)
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
#: ``bridge`` is the Bridge itself (sealed, or holding a setting that may not
#: open a door with only switched-on services allowed); ``owner_switch`` is the
#: owner's switch on "What leaves your computer".
FLAG_SOURCES = ("bridge", "env", "overlay", "owner_switch", "config", "default")

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


def _bridge_level() -> str:
    """The Bridge level in force: ``open``, ``switched_on`` or ``sealed``.

    Fails closed: when the Bridge cannot be read, only switched-on services.
    """
    try:
        from puffpai.security import bridge_guard
        return str(bridge_guard.reach_level())
    except Exception:
        return "switched_on"


def _bridge_up() -> bool:
    """True when only what the owner switched on may leave (fails closed)."""
    return _bridge_level() != "open"


def _owner_switch(door_id: str) -> Optional[bool]:
    """The owner's switch from ``outside_switches.json``, or None. Fails shut."""
    try:
        from puffpai.security import outside_switches
        return outside_switches.get(door_id)
    except Exception as exc:  # unreadable state: shut, never open
        logger.warning("owner switch for %s unreadable (%s); door stays shut",
                       _shown(door_id), type(exc).__name__)
        return False


def _resolve_flag(d: Door) -> tuple[bool | None, str, str]:
    """Resolve one door's flag. Returns ``(value_or_None, source, reason)``.

    The single precedence chain, used by both :func:`egress_allowed` and
    :func:`_raw_flag` so the two can never disagree:

    0. ``bridge``: sealed, every door is shut.
    1. ``env`` — ``PUFFPAI_EGRESS_*``. An operator forcing a door from the
       systemd unit must not be overridable by anything the running service can
       write, which is the whole reason this tier is first. With only
       switched-on services allowed it may switch a door off, never on.
    2. ``overlay`` and ``owner_switch``: the owner's own switches, in
       :func:`overlay_path` (Settings) and in ``outside_switches.json`` ("What
       leaves your computer"). When the two disagree, OFF wins.
    3. ``config`` — ``egress_doors`` in puffpai_config.json, then the door's
       ``legacy_config_key``. Shipped/provisioned state; with only
       switched-on services allowed it may switch a door off, never on.
    4. ``default`` — ``Door.default``, which is False for every door.

    ``None`` for the value means no tier expressed an opinion, so the caller
    applies ``Door.default``. Every ambiguity below resolves to False, never to
    True: a junk value, an unreadable overlay and an unreadable config are all
    "not consent".
    """
    level = _bridge_level()
    if level == "sealed":
        return False, "bridge", ("the Bridge lets nothing reach the "
                                 "internet, so every outside service stays off")
    if _is_feed_door(d):
        return _feed_door_flag(d, level)

    raw_env = os.environ.get(d.env_var)
    env_on = False
    if raw_env is not None and raw_env.strip() != "":
        decided = _as_bool(raw_env)
        if decided is None:
            return False, "env", (f"{d.env_var}={raw_env.strip()!r} is not a "
                                  f"flag value; door stays shut")
        if decided is False or level == "open":
            return decided, "env", (f"{d.env_var}="
                                    f"{'on' if decided else 'off'} "
                                    f"(environment wins)")
        env_on = True

    overlay = _raw_overlay()
    if overlay is None:
        return False, "overlay", (
            f"the owner overlay at {overlay_path()} will not parse; every "
            f"door stays shut until it is fixed or removed")
    chosen: bool | None = None
    if d.config_flag in overlay:
        chosen = _as_bool(overlay.get(d.config_flag))
        if chosen is None:
            return False, "overlay", (f"overlay {d.config_flag} is not a flag "
                                      f"value; door stays shut")
    owner = _owner_switch(d.id) if d.id in _BY_ID else None
    if chosen is False:
        return False, "overlay", (f"overlay {d.config_flag}=False "
                                  f"(set by the owner)")
    if owner is False:
        return False, "owner_switch", "the owner switched it off"
    if chosen is True:
        return True, "overlay", (f"overlay {d.config_flag}=True "
                                 f"(set by the owner)")
    if owner is True:
        return True, "owner_switch", "the owner switched it on"

    held = ("the Bridge lets only a switch the owner set open a door, so "
            "{what} cannot")
    section = _raw_config().get(CONFIG_SECTION)
    if isinstance(section, dict) and d.config_flag in section:
        decided = _as_bool(section.get(d.config_flag))
        if decided is None:
            return False, "config", (f"{CONFIG_SECTION}.{d.config_flag} is not "
                                     f"a flag value; door stays shut")
        if decided is False or level == "open":
            return decided, "config", f"{CONFIG_SECTION}.{d.config_flag}={decided}"
        return None, "bridge", held.format(
            what=f"{CONFIG_SECTION}.{d.config_flag} in the config file")

    if d.legacy_config_key:
        decided = _as_bool(_raw_config().get(d.legacy_config_key))
        if decided is not None:
            if decided is False or level == "open":
                return decided, "config", (
                    f"legacy key {d.legacy_config_key}={decided} "
                    f"(set {CONFIG_SECTION}.{d.config_flag} to move it)")
            return None, "bridge", held.format(
                what=f"the legacy key {d.legacy_config_key}")

    if env_on:
        return None, "bridge", held.format(what=d.env_var)
    return None, "default", (f"{CONFIG_SECTION}.{d.config_flag} is not set; "
                             f"default is {'open' if d.default else 'shut'}")


def _switch(d: Door) -> tuple[Optional[bool], str]:
    """The owner's explicit setting for this door and why: ``(value, reason)``.

    ``None`` means no tier gave one (the door stays shut). Same chain as
    :func:`egress_allowed`.
    """
    decided, _source, reason = _resolve_flag(d)
    return decided, reason


def _shut_code(d: "Door | None") -> str:
    """The outside-traffic log's word for why *d* is shut."""
    if d is None:
        return "not_on_list"
    try:
        source = "" if d.derived else _resolve_flag(d)[1]
    except Exception:
        source = ""
    if source == "bridge":
        return "bridge_up"
    if source == "env":
        return "turned_off_here"
    return "switched_off"


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


def probe_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    """One TCP connect to a probe target, made here so it passes this exit.

    Only a ``(host, port)`` that :func:`probe_targets` returns right now is
    dialled: a host of a door that is open, on the probe port. Every answer
    the name looks up to must be on the internet (an answer inside the house
    is refused, the same rule as every other request here) and the socket
    goes to an address that just passed. Nothing is sent on the connection;
    it is closed at once. Anything else answers False without opening a
    socket.
    """
    if not any((host, port) == (h, p) for h, p, _door in probe_targets()):
        return False
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addresses = [info[4] for info in infos]
    if not addresses:
        return False
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(str(addr[0]).split("%", 1)[0])
        except ValueError:
            return False
        if not ip.is_global or _internal_reason(str(ip)):
            return False  # the same rule as every exit (NAT64, 6to4, Teredo)
    for info in infos:
        try:
            sock = socket.socket(info[0], info[1], info[2])
        except OSError:
            continue
        try:
            sock.settimeout(timeout)
            sock.connect(info[4])
            return True
        except OSError:
            continue
        finally:
            sock.close()
    return False


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
        raise EgressBlocked(door_id, reason, message, code=_shut_code(d))
    return d  # type: ignore[return-value]


def shut_reason(door_id: str) -> str:
    """The one-line machine-readable reason a door is shut ('' if it is open)."""
    allowed, reason = egress_allowed(door_id)
    return "" if allowed else reason


def settings_hint(door_id: str) -> str:
    """The sentence to show a person when this door is shut."""
    d = door(door_id)
    return d.settings_hint if d is not None and d.settings_hint else (
        "This bot has no outside service by that name, so nothing was sent.")


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


def _check_addresses(d: Door, host: str, hop: bool = False,
                     resolver=None) -> tuple:
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
            host, ALLOWED_PORT, posture=_posture(d),
            resolver=resolver or _resolver())
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


def _pinned_session(d: Door, resolver=None):
    """A ``requests`` session that can only dial an address we just validated.

    Separated so a test can substitute a transport, and so the failure to build
    one is a refusal rather than a silent fall-back to an unpinned client: the
    pin is the half of this control that survives a hostile resolver, and a
    caller that believes it is pinned when it is not is worse off than one that
    knows it is not.
    """
    return netaddr.pinned_session(posture=_posture(d),
                                  resolver=resolver or _resolver())


# ── the wrapper ──────────────────────────────────────────────────────────────

def egress_request(door_id: str, method: str, url: str, *,
                   timeout: float = DEFAULT_TIMEOUT,
                   follow_redirects: bool = True, resolver=None,
                   credential: Optional["_BoundCredential"] = None, **kwargs):
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

    **A key rides only as** ``credential=``, a :class:`_BoundCredential` only
    code inside ``puffpai.security`` can construct. With one present: https
    only, the destination host must be one the credential is bound to (exact,
    IDN-normalised), **zero redirects** (a 3xx is a refusal), and the header is
    stripped from ``response.request.headers`` before the response is
    returned. On a request carrying a credential, and on every keyed or feed
    door, a caller-supplied ``auth=``, ``cookies=`` or credential-shaped header
    is refused. A host a door names for pictures only gets a plain GET answered
    by a size-capped image. :data:`RESULT_PICTURES` is reached only through
    :func:`result_picture_get`. *resolver* replaces the address lookup for this
    request (tests).
    """
    scope = kwargs.pop("picture_scope", None)
    if str(door_id) == RESULT_PICTURES or scope is not None:
        return _picture_request(door_id, method, url, scope, kwargs, credential,
                                timeout=timeout, resolver=resolver)
    known = door(door_id)
    try:
        if (credential is not None or str(door_id) in KEYED_DOORS
                or _is_feed_door(known)):
            # Authority is not a kwarg on these: a key rides only as credential=.
            _screen_kwargs(str(door_id), kwargs)
        cred = (_check_credential(str(door_id), credential)
                if credential is not None else None)
        d = require_open(door_id)
    except EgressBlocked as blocked:
        _LedgerTag.for_door(known, str(door_id), url).refused(blocked, url, method)
        raise
    if resolver is not None:
        _check_url(d, url, resolver=resolver)
    else:
        _check_url(d, url)
    origin_host = _host_of(url)
    picture_only = origin_host in d.picture_hosts and origin_host not in d.hosts
    if picture_only:
        try:
            _picture_request_ok(d, method, kwargs, credential)
        except EgressBlocked as blocked:
            _LedgerTag.for_door(d, d.id, url).refused(blocked, url, method)
            raise
        kwargs["stream"] = True

    if cred is not None:
        # _check_url already refused anything but https; restated here so the
        # credential rule does not depend on a door rule staying as it is.
        blocked = None
        if urlsplit(url.strip()).scheme.lower() != "https":
            blocked = EgressBlocked(
                d.id, "credential requested on a non-https url",
                "That request was refused because it was not an encrypted "
                "connection.", code="not_encrypted")
        elif _ascii_host(origin_host) not in cred.hosts:
            blocked = EgressBlocked(
                d.id, f"credential is not bound to host {origin_host!r}",
                "That request tried to send a sign-in to an address it is not "
                "registered for, so nothing was sent.", code="sign_in_refused")
        if blocked is not None:
            _LedgerTag.for_door(d, d.id, url).refused(blocked, url, method)
            raise blocked
        # Attached last, on a copy, so the value lives in no caller's dict.
        kwargs["headers"] = {**(kwargs.get("headers") or {}),
                             cred.header_name: cred._value}

    kwargs.pop("allow_redirects", None)  # this function owns hop policy
    # Built after the decision. The resolver goes along only when the caller
    # named one, so a substitute transport keeps its one-argument shape.
    session = (_pinned_session(d, resolver=resolver) if resolver is not None
               else _pinned_session(d))
    # A session is per request: pooled connections are keyed by host name, and
    # a reused connection is one whose address was validated at some earlier
    # time under some earlier DNS answer. No door here is high enough volume for
    # the extra handshake to matter.
    keep_open = False

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
                        host=host, path=path, kind="request_failed",
                        method=method)
                raise
            if pinned_refusal is not None:
                raise pinned_refusal
            redirected = resp.status_code in (301, 302, 303, 307, 308)
            if cred is not None:
                _strip_credential_header(resp, cred.header_name)
                if redirected:
                    # Not a hop. The next target is the server's choice, and a
                    # credential follows only where it was bound.
                    _close_quietly(resp)
                    host, path = _safe_parts(current)
                    _record(door_id, ok=False,
                            detail="authenticated request was redirected",
                            host=host, path=path, kind="url_check",
                            method=method, code="sign_in_refused")
                    raise EgressBlocked(
                        d.id, "authenticated request was redirected",
                        "That address redirected while carrying a sign-in, so "
                        "nothing further was sent.", code="sign_in_refused")
            elif follow_redirects and redirected:
                location = resp.headers.get("Location") or ""
                # Done with this hop: release its connection now, before the
                # next hop is checked (and possibly refused), so a streamed
                # redirect chain leaks nothing back into the session.
                _close_quietly(resp)
                nxt = urljoin(current, location)
                # Re-validate before following. A redirect off the door's host
                # set — or onto a host that resolves indoors — is refused here
                # rather than followed and judged afterwards.
                if resolver is not None:
                    _check_url(d, nxt, hop=True, resolver=resolver)
                else:
                    _check_url(d, nxt, hop=True)
                nxt_host = _host_of(nxt)
                if (not picture_only and nxt_host in d.picture_hosts
                        and nxt_host not in d.hosts):
                    # A hop onto a pictures-only host: from here, a picture or
                    # nothing.
                    picture_only = True
                    _picture_request_ok(d, method, kwargs, credential)
                    kwargs["stream"] = True
                current = nxt
                continue
            host, path = _safe_parts(current)
            _record(door_id, ok=True,
                    detail=f"{method.upper()} {host}",
                    host=host, path=path,
                    bytes_out=_request_bytes(resp),
                    bytes_in=_declared_length(resp),
                    status=getattr(resp, "status_code", None),
                    method=method)
            if picture_only:
                return _picture_answer(d, resp)
            if kwargs.get("stream"):
                # The body has not been read yet, so the session must outlive
                # this function.
                keep_open = True
                try:
                    resp._puffpai_egress_session = session
                except Exception:  # pragma: no cover - transport specific
                    pass
            return resp
    finally:
        if not keep_open:
            closer = getattr(session, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # pragma: no cover - transport specific
                    pass

    _record(door_id, ok=False, detail="too many redirects",
            host=_host_of(current), method=method, code="too_many_redirects")
    raise EgressBlocked(
        d.id, f"more than {MAX_REDIRECTS} redirects",
        f"{d.settings_hint or 'That request'} — the site redirected too many "
        f"times, so nothing was fetched.", code="too_many_redirects")


def _close_quietly(resp) -> None:
    closer = getattr(resp, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # pragma: no cover - transport specific
            pass


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


def app_egress_get(app_id: str, index, url: Optional[str] = None, **kwargs):
    """``GET`` through an app's declared door. See :func:`app_egress_request`.

    Called as ``(app_id, url)`` it goes through the app's live feed door
    (``app:<app_id>:feed``), the one outbound path for a feed.
    """
    if url is None:
        return egress_request(app_door_id(str(app_id or "").strip()), "GET",
                              index, **kwargs)
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
    return EgressBlocked(d.id, reason, user_message,
                         code=_outside_code(reason, reason_code))


def _check_url(d: Door, url: str, hop: bool = False, resolver=None) -> None:
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
    if host not in d.hosts and host not in d.picture_hosts:
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
    _check_addresses(d, host, hop=hop, resolver=resolver)


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
            address_class: str = "", method: str = "GET",
            code: str = "") -> None:
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

    _outside_line(door_id, ok, kind, host=host, path=path, method=method,
                  status=status, bytes_in=bytes_in, bytes_out=bytes_out,
                  detail=entry["detail"], reason_code=reason_code, code=code)


def _outside_line(door_id: str, ok: bool, kind: str, *, host: str, path: str,
                  method: str, status: int | None, bytes_in: int | None,
                  bytes_out: int | None, detail: str, reason_code: str,
                  code: str) -> None:
    """One line in the outside-traffic log for a door decision. Never raises.

    The decision ledger above is the household's audit trail; this is the
    plain-words list on "What leaves your computer". A refusal reached before
    any address was known (a shut switch checked by a call site) writes none.
    """
    if kind == "door_check":
        return
    try:
        from puffpai.security import egress_ledger
        key = str(door_id or "").strip().lower()
        if ok and kind == "request_failed":
            outcome, word = "failed", "no_answer"
        elif ok:
            outcome, word = "done", ""
        else:
            outcome = "refused"
            word = code or _outside_code(detail, reason_code)
        egress_ledger.record(
            _ledger_service(key), host=egress_ledger.safe_host(host),
            outcome=outcome, method=str(method or "GET"),
            path=(path if key in _PATH_KEPT else ""), code=word,
            status=status, bytes_in=bytes_in, bytes_out=bytes_out,
            what=_WHAT_BY_DOOR.get(key, "card_data" if _is_feed_door(key)
                                   else "request"))
    except Exception as exc:  # the log must never break a request
        logger.debug("outside-traffic line skipped for %s: %s", door_id,
                     type(exc).__name__)


def _outside_code(detail: str, reason_code: str = "") -> str:
    """The outside-traffic log's word for a refusal recorded here."""
    if reason_code == REASON_RESOLVED_PRIVATE:
        return "inside_address"
    if reason_code == REASON_UNRESOLVABLE:
        return "cannot_look_up"
    return _code_from_text(detail)


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
    posture = run_posture()
    return {
        "doors": door_status(),
        "posture": posture["run_egress"],
        "posture_source": posture["source"],
        "open_posture": OPEN_POSTURE,
    }


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
            # The same rule as egress_admin.toggleable: a door with a flag of
            # its own that the registry reads (result_pictures goes through
            # egress_request too, so its switch is real).
            "writable": ((d.enforcement in ("registry", "derived")
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

# ===========================================================================
# The build engine's outside paths
# ===========================================================================

# ── URL rules for a dynamic-host door ──────────────────────────────────────────

def _port_of(url: str) -> int:
    """The port a URL will connect on. Every door here is https."""
    try:
        return urlsplit(str(url)).port or 443
    except ValueError:
        return 443


def _internal_reason(addr_text: str) -> str:
    """Why this address is internal, or '' if it is globally routable.

    Judged with :mod:`ipaddress` on the address itself rather than against a
    list of strings, because a blocklist of ``127.0.0.1``/``10.*`` never covers
    ``0.0.0.0``, ``[::1]``, ``::ffff:127.0.0.1`` or the next range someone
    invents. ``is_global`` is the positive test; the named properties exist so
    the refusal can explain itself.
    """
    try:
        addr = ipaddress.ip_address(str(addr_text).split("%", 1)[0])
    except ValueError:
        return ""
    candidates = [addr]
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        candidates.append(mapped)  # ::ffff:127.0.0.1 is loopback in a costume
    for attribute, description in (
            ("is_loopback", "is loopback"),
            ("is_link_local", "is link-local (the cloud-metadata range)"),
            ("is_multicast", "is multicast"),
            ("is_unspecified", "is unspecified"),
            ("is_private", "is a private address on this network"),
            ("is_reserved", "is reserved")):
        if any(getattr(c, attribute, False) for c in candidates):
            return description
    if not all(c.is_global for c in candidates):
        return "is not globally routable"
    # The pinned-connection policy also unwraps 6to4, Teredo and NAT64 forms;
    # one policy for every exit.
    from puffpai.security.pinned_net import address_refusal
    return address_refusal(str(addr))


def internal_address_reason(addr_text: str) -> str:
    """Public name for :func:`_internal_reason`. '' means globally routable.

    Exported so a caller that does its own pre-flight refusal (the Toolsmith's
    ``web_fetch``, which must reject a literal or a resolved answer *before*
    this module is asked to open anything) judges an address by exactly the
    same policy the doors do, rather than growing a second, drifting copy.
    A non-address string returns '', it is a hostname, not an address, and
    naming it here is a different check.
    """
    return _internal_reason(addr_text)


def resolve_addresses(host: str, port: int = 443) -> list[str]:
    """Every address *host* answers with. Public seam for pre-flight checks."""
    return _default_resolver(host, port)


def _is_private_host(host: str) -> bool:
    """True for a *literal* address that is not globally routable."""
    return bool(_internal_reason(host))


def _default_resolver(host: str, port: int) -> list[str]:
    """Every address *host* answers with. Injected in tests; never called there."""
    infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    # sockaddr[0] is the address for both AF_INET and AF_INET6; str() is for
    # the type checker, which sees the whole sockaddr union.
    return [str(info[4][0]) for info in infos]


def _check_resolved_public(d: Door, host: str, port: int,
                           resolver=None) -> list[str]:
    """Resolve *host* and refuse unless EVERY answer is globally routable.

    A dynamic-host door takes an address the owner supplied, so it cannot be
    held to a fixed host allowlist. What it can be held to is where that name
    actually points. Checking the *name* alone is not a control: ``feeds.evil``
    can answer ``169.254.169.254``, and a widget URL is reachable by anything
    that can call ``dashboard_add_widget``.

    Every answer must pass, not merely the first: a name that resolves to one
    public and one private address is refused, because the stack picks which
    one the socket gets. Returns the vetted answers: :func:`egress_request`
    PINS the connection to the first of them (:mod:`pinned_net`), so the name
    is never looked up a second time between this check and the connect --
    that second lookup was the DNS-rebinding window.
    """
    try:
        addresses = (resolver or _default_resolver)(host, port)
    except OSError as exc:
        raise EgressBlocked(
            d.id, f"cannot resolve {host!r} ({type(exc).__name__})",
            f"That address could not be looked up, so nothing was sent.")
    if not addresses:
        raise EgressBlocked(
            d.id, f"{host!r} resolves to nothing",
            f"That address could not be looked up, so nothing was sent.")
    from puffpai.security import pinned_net
    if pinned_net.filter_block(addresses, host):
        raise EgressBlocked(
            d.id, f"{host} was blocked by the network's filter",
            "That website is blocked by your network's filter, so nothing "
            "was sent.", code="network_filter")
    for addr in addresses:
        reason = _internal_reason(addr)
        if reason:
            raise EgressBlocked(
                d.id, f"{host} resolves to {addr}, which {reason}",
                f"That request tried to reach {host}, which points at an "
                f"address on this machine or this network, so nothing was "
                f"sent.", code="inside_address")
    return [str(a) for a in addresses]


def _verify_peer(d: Door, response, host: str) -> None:
    """Belt and braces: re-check the peer when the transport exposes it.

    The control that closes DNS rebinding is the PIN (:mod:`pinned_net`): the
    socket is opened to the address :func:`_check_resolved_public` vetted, and
    the pinned connection itself compares the socket's real peer with that
    address before a byte is sent. With urllib3 2.6 ``response.raw`` does not
    expose the socket, so this after-the-fact check usually has nothing to
    look at and returns; it stays for transports (and tests) that do.
    """
    peer = None
    try:
        peer = response.raw._connection.sock.getpeername()[0]
    except Exception:
        peer = getattr(response, "peer_address", None)
    if peer is None:
        return
    reason = _internal_reason(str(peer))
    if reason:
        # The bytes are already on the wire; the body is never handed back.
        raise EgressBlocked(
            d.id, f"connection to {host} landed on {peer}, which {reason}",
            f"That request reached an address on this machine or this "
            f"network, so the reply was discarded.")


def _dynamic_check_url(d: Door, url: Any, hop_host: str = "") -> str:
    """Refuse anything the door does not cover. Opens nothing. Returns the host.

    ``hop_host`` is set when re-validating a redirect target: for a dynamic
    door the hop must stay on the host the owner originally named.
    """
    if not isinstance(url, str) or not url.strip():
        raise EgressBlocked(d.id, "empty url",
                            "That request had no address to fetch.")
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise EgressBlocked(d.id, "unparseable url",
                            "That web address could not be read.")

    host = _clean_host(parts.hostname or "")

    if parts.scheme.lower() != "https":
        # Every door here is an https endpoint. Refusing http outright means a
        # downgrade cannot be introduced by a redirect or a hand-edited config.
        raise EgressBlocked(
            d.id, f"scheme {parts.scheme!r} is not https",
            "That request was refused because it was not an encrypted "
            "connection.")
    if parts.username or parts.password:
        # The reason string must not echo the credentials back into a log.
        raise EgressBlocked(d.id, "url carries credentials",
                            "That web address was refused.")
    if not host:
        raise EgressBlocked(d.id, "url has no host",
                            "That web address was refused.")
    if is_app_door(d) and _port_of(url) != 443:
        # An app's door was declared by a manifest, not reviewed by a human,
        # so it gets the default https port and nothing else.
        raise EgressBlocked(
            d.id, f"port {_port_of(url)} is not 443",
            "That request was refused because it used an unusual port.")

    if d.dynamic_hosts:
        if _is_private_host(host):
            raise EgressBlocked(
                d.id, f"host {host!r} is a private or loopback address",
                f"That request tried to reach {host}, which is an address on "
                f"this machine or this network, so nothing was sent.")
        if hop_host and host != hop_host:
            raise EgressBlocked(
                d.id, f"redirect to {host!r} leaves the requested host "
                      f"{hop_host!r}",
                f"That feed redirected to {host}, which is not where you "
                f"pointed it, so nothing was fetched.")
        return host

    if host not in d.hosts and host not in d.picture_hosts:
        raise EgressBlocked(
            d.id,
            f"host {host!r} is not declared by door {d.id!r}; declared: "
            f"{', '.join(d.hosts)}",
            f"That request tried to reach {host}, which is not one of the "
            f"addresses the {d.id!r} service covers, so nothing was sent.")
    return host


# ── credentials ──────────────────────────────────────────────────────────────
#
# A door says *where* a request may go. Until now it said nothing about *what
# authority rides along*: ``egress_request`` forwarded ``**kwargs`` straight to
# ``requests.request``, so any in-process caller could attach an
# ``Authorization`` header and the door layer would carry it through redirect
# hops without ever knowing a credential was on the wire. The rules below make
# that impossible by default and give a credential exactly one way in.

#: Header names that always carry authority, refused from callers outright.
_CREDENTIAL_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "x-auth-token",
})

#: Substrings that make a header name credential-shaped. Deliberately broad:
#: a false refusal on ``X-Monkey-Id`` costs one explicit exception here, a
#: false accept on ``X-Session-Key`` costs a leaked credential.
_CREDENTIAL_SUBSTRINGS = ("auth", "token", "secret", "key", "cookie")

_SECURITY_PACKAGE = __name__.rsplit(".", 1)[0]  # "puffpai.security"

#: The mark :meth:`_BoundCredential.__init__` puts on every credential it
#: builds. An object forged with ``object.__new__`` has no mark, so presenting
#: one to :func:`egress_request` is refused even though its type matches.
#: A mark on the object, NOT a module-wide list of issued credentials: such a
#: list would let any code in the process walk it and read a key while its
#: request is in flight.
_ISSUE_MARK = object()


def _is_credential_header(name: Any) -> bool:
    """True when *name* looks like it carries authority. Errs toward True."""
    if not isinstance(name, str):
        return True  # cannot be screened, so it does not go on the wire
    low = name.strip().lower()
    if low in _CREDENTIAL_HEADERS:
        return True
    return any(part in low for part in _CREDENTIAL_SUBSTRINGS)


def _ascii_host(host: Any) -> str:
    """Host normalised for comparison: lower-case, no trailing dot, IDN→ASCII.

    Both sides of the bound-host check go through this, so a credential bound
    to ``example.com`` is not handed to ``exämple.com`` and vice versa.
    """
    cleaned = _clean_host(host)
    if not cleaned:
        return ""
    try:
        return cleaned.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        # Not an IDN-encodable name (literal IP, underscore label, over-long
        # label). Comparison then happens on the cleaned form, which is the
        # same transformation applied to the bound hosts.
        return cleaned


class _BoundCredential:
    """One header's worth of authority, bound to the hosts it may reach.

    **This class is the single sanctioned way a credential reaches the wire.**
    The key broker (:mod:`puffpai.security.keys`) mints these through
    :func:`_bind_credential`, and the rules in :func:`egress_request` govern
    every one:

    * TLS only, and the destination host must be one of ``hosts``, exact
      match, IDN-normalised to ASCII, never a wildcard or a suffix match;
    * zero redirects: any 3xx is a refusal, not a hop, so the header is never
      offered to a target the server chose;
    * the header is attached immediately before the call and stripped from
      ``response.request.headers`` before the response is handed back;
    * the value never appears in a log line, a refusal message or a repr.

    It cannot be constructed from outside ``puffpai.security``: ``__init__``
    checks the calling module. That is a guard against accident and against an
    LLM-driven or plugin call site talking itself into attaching a token, not
    a sandbox. Any code already running in this process can monkeypatch this
    module; the boundary this defends is between the security package and the
    rest of the application, not between the application and an attacker with
    code execution.
    """

    __slots__ = ("_header", "_value", "_hosts", "_mark", "__weakref__")

    def __init__(self, header_name: str, value: str,
                 hosts: "tuple[str, ...] | list[str] | set[str]"):
        caller = sys._getframe(1).f_globals.get("__name__", "")
        if not (caller == _SECURITY_PACKAGE
                or caller.startswith(_SECURITY_PACKAGE + ".")):
            raise PermissionError(
                "_BoundCredential may only be constructed inside "
                f"{_SECURITY_PACKAGE!r}; {caller!r} must ask the security "
                "package for a bound credential instead.")

        if not isinstance(header_name, str) or not header_name.strip():
            raise ValueError("credential header name must be a non-empty string")
        header = header_name.strip()
        if not header.isascii() or any(c in header for c in "\r\n\t :"):
            # A header name carrying a separator is header injection.
            raise ValueError("credential header name is not a valid header name")

        if not isinstance(value, str) or not value:
            # No value in the message: this exception may be logged.
            raise ValueError("credential value must be a non-empty string")
        if "\r" in value or "\n" in value:
            raise ValueError("credential value contains a line break")

        if isinstance(hosts, str) or not hosts:
            raise ValueError("credential hosts must be a non-empty collection")
        bound = set()
        for h in hosts:
            if not isinstance(h, str) or not h.strip():
                raise ValueError("credential host entries must be non-empty strings")
            if "*" in h:
                raise ValueError("credential hosts are exact; no wildcards")
            bound.add(_ascii_host(h))
        if "" in bound:
            raise ValueError("credential host entries must be non-empty strings")

        self._header = header
        self._value = value
        self._hosts = frozenset(bound)
        self._mark = _ISSUE_MARK

    @property
    def header_name(self) -> str:
        return self._header

    @property
    def hosts(self) -> frozenset:
        return self._hosts

    def __repr__(self) -> str:  # never the value
        return (f"<_BoundCredential header={self._header!r} "
                f"hosts={sorted(self._hosts)!r}>")

    __str__ = __repr__


#: Where a bound credential may ride. Header only: a
#: key in a query string is copied into every log, proxy and exception text
#: that ever sees the address, so v1 refuses it outright.
CREDENTIAL_PLACEMENTS = ("header",)


def _bind_credential(header_name: str, value: str, hosts, *,
                     placement: str = "header") -> _BoundCredential:
    """Module-private factory for the credential broker
    (:mod:`puffpai.security.keys`).

    Not exported, and -- like the constructor -- refused to any caller outside
    ``puffpai.security``: without this check the factory itself was a way
    round the constructor's guard, because the constructor only saw this
    module as its caller. *placement* must be ``"header"``; anything else
    (``"query"``) raises before a credential exists.
    """
    caller = sys._getframe(1).f_globals.get("__name__", "")
    if not (caller == _SECURITY_PACKAGE
            or caller.startswith(_SECURITY_PACKAGE + ".")):
        raise PermissionError(
            "_bind_credential may only be called inside "
            f"{_SECURITY_PACKAGE!r}; {caller!r} must use the key broker "
            "(puffpai.security.keys) instead.")
    if placement not in CREDENTIAL_PLACEMENTS:
        # No value in the message: this exception may be logged.
        raise ValueError("a key may only travel in a request header")
    return _BoundCredential(header_name, value, hosts)


def _screen_kwargs(door_id: str, kwargs: dict) -> None:
    """Refuse caller-supplied authority before anything touches the network.

    Refusal messages name the *header*, never its value, because they are
    written to a log and shown to a person.
    """
    if "auth" in kwargs:
        raise EgressBlocked(
            door_id, "caller supplied an auth= argument",
            "That request tried to attach a sign-in to an outbound call, "
            "which this connection does not carry.")
    if kwargs.get("cookies"):
        raise EgressBlocked(
            door_id, "caller supplied cookies=",
            "That request tried to attach a sign-in to an outbound call, "
            "which this connection does not carry.")

    headers = kwargs.get("headers")
    if headers is None:
        return
    if not isinstance(headers, Mapping):
        # Unscreenable, so it does not go out. requests accepts a sequence of
        # pairs; a caller that needs that can pass a dict.
        raise EgressBlocked(
            door_id, f"headers must be a mapping, got {type(headers).__name__}",
            "That request was refused because its headers could not be "
            "checked.")
    for name in headers:
        if _is_credential_header(name):
            shown = name if isinstance(name, str) else type(name).__name__
            raise EgressBlocked(
                door_id, f"caller supplied credential-bearing header {shown!r}",
                "That request tried to attach a sign-in to an outbound call, "
                "which this connection does not carry.")


def _check_credential(door_id: str, credential: Any) -> _BoundCredential:
    """Refuse anything that is not a credential this package issued."""
    if type(credential) is not _BoundCredential \
            or getattr(credential, "_mark", None) is not _ISSUE_MARK:
        raise EgressBlocked(
            door_id, "credential was not issued by the security package",
            "That request tried to attach a sign-in that this system did not "
            "issue, so nothing was sent.")
    return credential


def _strip_credential_header(resp: Any, header_name: str) -> None:
    """Remove the credential from the response's record of the request."""
    try:
        headers = getattr(getattr(resp, "request", None), "headers", None)
        if headers is None:
            return
        for key in [k for k in headers
                    if isinstance(k, str) and k.lower() == header_name.lower()]:
            del headers[key]
    except Exception:  # a stub transport with an odd request object
        logger.debug("could not strip credential header from response.request")


# ── a sign-up key on one source_fetch.fetch_small request ────────────────────
#
# ``source_fetch.fetch_small(..., key_id="github")`` may carry a sign-up key.
# The WHOLE keyed request happens here, inside the security package:
# :func:`keyed_first_hop` checks the owner's switch for the service, reads the
# key, binds it with :func:`_bind_credential` to the hosts the curated row,
# the key index AND the stored record all name (exact, IDN-normalised), opens
# its own PINNED connection (never a caller's transport), sends, reads at most
# the size the caller allows, removes any echo of the key from the body and
# the answer headers, writes the one outside-traffic line for the hop (every
# outcome, refusals included) and hands back a plain answer object that holds
# no credential.
#
# Where the key lives while a request is in flight: ONLY in the
# local variables of the one :func:`keyed_first_hop` call and the request
# objects it builds. No module-level table, list or cache in this package
# holds the key, its header value, the words removed from the answer, or a
# bound credential (the issued-credential list became a mark on the object
# for the same reason). The helpers that touch the value
# (:func:`_fetch_key_credential`, :func:`_credential_header_for`) refuse any
# caller outside the package, the same guard :func:`_bind_credential` has.
#
# Other rules, as ``keys.keyed_get``: the curated row must exist, a key that
# was rejected or is over its caps is not used, a card-backed key never works
# with the vault locked, the key rides ONLY the first hop (a redirect never
# gets it), and one use is counted only when a request is about to go out.
# The header value follows the row's ``placement.value_format`` (default
# ``{key}``; GitHub: ``Bearer {key}``).
#
# Residual, stated plainly:
# * The switch check and the log line are enforced HERE, not by trusting the
#   caller, so they hold even for a caller that gets past the caller check
#   below (that check reads the calling frame, which code running ``exec``
#   with made-up globals can imitate).
# * The service name is the caller's choice, as it is for every fetch_small
#   call: a key is bound to hosts, not to one service. A caller that names a
#   service whose switch is on can spend the key's per-minute and monthly
#   allowance against its bound hosts, and the log shows that service.
# * Python gives code in the same process ways to read another call's local
#   variables (``gc``, ``sys._current_frames``, ``ctypes``). Those, and
#   monkeypatching this module, are out of scope, as for every guard here:
#   the boundary kept is that no ordinary name or function outside
#   ``puffpai.security`` hands out the key.

_FETCH_KEY_DOOR = "fetch_small"
_MAX_VALUE_FORMAT = 64
_KEYED_MAX_READ = 64 * 1024 * 1024
#: The functions outside this package that may send a keyed hop:
#: (module, qualified name) of the closure ``fetch_small`` builds for one
#: request, and the one ``get_archive`` builds for one download (a
#: private project of the owner's, through GitHub's archive address). Not the module
#: as a whole: a module-level helper there would be a way round the switch
#: check.
_KEYED_HOP_CALLER = ("puffpai.source_fetch", "fetch_small.<locals>._send_keyed")
_KEYED_HOP_CALLERS = frozenset({
    _KEYED_HOP_CALLER,
    ("puffpai.source_fetch", "get_archive.<locals>._send_keyed"),
})
#: The ledger id a keyed hop is logged under when its service is not known
#: (the caller's own text never becomes a service id).
_KEYED_UNKNOWN_SERVICE = "fetch_small"
_KEYED_REDIRECTS = (301, 302, 303, 307, 308)
#: Shortest tail of a key that is still removed when a body is cut mid-key.
_MIN_ECHO_TAIL = 6


def _require_security_caller(what: str) -> None:
    """Refuse when the function that called *what* is outside the package."""
    caller = sys._getframe(2).f_globals.get("__name__", "")
    if not (caller == _SECURITY_PACKAGE
            or caller.startswith(_SECURITY_PACKAGE + ".")):
        raise PermissionError(
            f"{what} may only be called inside {_SECURITY_PACKAGE!r}; "
            f"{caller!r} must use source_fetch.fetch_small(key_id=...) instead.")


def _value_format_ok(fmt: Any) -> bool:
    if not isinstance(fmt, str) or len(fmt) > _MAX_VALUE_FORMAT:
        return False
    if fmt.count("{key}") != 1:
        return False
    rest = fmt.replace("{key}", "")
    return rest.isascii() and all(32 <= ord(ch) < 127 for ch in rest) \
        and "{" not in rest and "}" not in rest


def fetch_key_hosts(key_id: Any) -> Optional[frozenset]:
    """The hosts the curated key row *key_id* may go to, or None (no such
    usable row). Reads no vault and sends nothing."""
    from puffpai.security import keys
    try:
        row = keys.spec(key_id)
    except Exception:
        return None
    if not isinstance(row, dict):
        return None
    hosts = frozenset(_ascii_host(h) for h in (row.get("hosts") or [])
                      if isinstance(h, str))
    return frozenset(h for h in hosts if h) or None


def _fetch_key_credential(key_id: Any, host: Any, *, caller: str,
                          only_target: bool = False
                          ) -> "tuple[Optional[_BoundCredential], str, tuple[bytes, ...]]":
    """``(credential, "ok", echoes)`` for one keyed request to *host*, else
    ``(None, "key_host" | "key_unavailable", ())``. Counts one use when
    issued. *echoes* are the byte strings to remove from the answer (the key
    and its header value); the caller keeps them in a local variable only.

    Package-private: refused (``PermissionError``) to any caller outside
    ``puffpai.security``. With *only_target* the credential is bound to
    *host* alone (a browser app's key: exactly the host it was asked for).
    """
    _require_security_caller("_fetch_key_credential")
    from puffpai.security import keys
    sid = str(key_id or "")
    target = _ascii_host(host)
    try:
        row = keys.spec(sid)
    except Exception:
        row = None
    if not isinstance(row, dict) or not target:
        return None, "key_unavailable", ()
    row_hosts = {_ascii_host(h) for h in (row.get("hosts") or [])
                 if isinstance(h, str)}
    if target not in row_hosts:
        keys._audit("credential_use", caller, "refused", f"{sid} {target} host")  # noqa: SLF001
        return None, "key_host", ()
    placement = row.get("placement") or {}
    fmt = placement.get("value_format", "{key}")
    if not _value_format_ok(fmt) or placement.get("in") != "header":
        return None, "key_unavailable", ()
    try:
        entry = keys._read_index().get(sid)  # noqa: SLF001 - same package
    except Exception:
        entry = None
    if not isinstance(entry, dict) or entry.get("rejected_at"):
        return None, "key_unavailable", ()
    entry_hosts = {_ascii_host(h) for h in (entry.get("hosts") or [])
                   if isinstance(h, str)}
    if target not in entry_hosts:
        keys._audit("credential_use", caller, "refused", f"{sid} {target} host")  # noqa: SLF001
        return None, "key_host", ()
    cap = row.get("monthly_cap")
    if cap is not None:
        used = keys.uses_this_month(sid)
        if used is None or used >= cap:
            return None, "key_unavailable", ()
    vault = keys._vault(None)  # noqa: SLF001
    is_open = keys._is_open(vault)  # noqa: SLF001
    if not is_open and (row.get("card_required")
                        or entry.get("card_required") is not False
                        or not entry.get("background")
                        or row.get("vault_only")):
        keys._audit("credential_use", caller, "refused", f"{sid} {target} locked")  # noqa: SLF001
        return None, "key_unavailable", ()
    record = keys._record_for(vault, sid)  # noqa: SLF001
    raw = None
    try:
        if record is None or (record.get("card_required") is True and not is_open):
            return None, "key_unavailable", ()
        record_hosts = {_ascii_host(h) for h in (record.get("hosts") or [])
                        if isinstance(h, str)}
        if target not in record_hosts:
            keys._audit("credential_use", caller, "refused", f"{sid} {target} host")  # noqa: SLF001
            return None, "key_host", ()
        if not keys._per_minute_ok(sid, row):  # noqa: SLF001
            return None, "key_unavailable", ()
        bound = sorted(row_hosts & entry_hosts & record_hosts)
        if only_target:
            bound = [target] if target in bound else []
        if not bound:
            return None, "key_host", ()
        raw = record.get("value")
        if not isinstance(raw, str) or not raw:
            return None, "key_unavailable", ()
        try:
            cred = _bind_credential(str(placement.get("name") or ""),
                                    fmt.replace("{key}", raw), bound)
        except (ValueError, TypeError):
            return None, "key_unavailable", ()
        keys._count_use(sid)  # noqa: SLF001 - counted before sending
        echoes = tuple({raw.encode("utf-8"),
                        fmt.replace("{key}", raw).encode("utf-8")})
        keys._audit("credential_use", caller, "issued", f"{sid} {target}")  # noqa: SLF001
        return cred, "ok", echoes
    finally:
        record = None
        raw = None


def _credential_header_for(credential: Any, url: Any) -> Optional[tuple[str, str]]:
    """``(header, value)`` to attach for *url*, or None when *url* is not a
    plain https address on one of the credential's own hosts. Raises
    :class:`EgressBlocked` for a credential this package did not issue.

    Package-private: refused (``PermissionError``) to any caller outside
    ``puffpai.security``.
    """
    _require_security_caller("_credential_header_for")
    cred = _check_credential(_FETCH_KEY_DOOR, credential)
    try:
        parts = urlsplit(str(url))
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme or "").lower() != "https" or port not in (None, 443) \
            or parts.username is not None or parts.password is not None \
            or "@" in (parts.netloc or ""):
        return None
    if _ascii_host(parts.hostname or "") not in cred.hosts:
        return None
    return cred.header_name, cred._value  # noqa: SLF001 - the one use


def _credential_scrub(echoes: "tuple[bytes, ...]", data: bytes) -> bytes:
    """*data* with every one of *echoes* replaced, including one cut short at
    the very end of *data*. Returns bytes only."""
    if not isinstance(data, (bytes, bytearray)):
        return data
    out = bytes(data)
    for needle in echoes or ():
        if not needle:
            continue
        if needle in out:
            out = out.replace(needle, b"[removed]")
        # A body read up to a size limit can end part-way through the key.
        for k in range(len(needle) - 1, _MIN_ECHO_TAIL - 1, -1):
            if out.endswith(needle[:k]):
                out = out[:-k] + b"[removed]"
                break
    return out


def _fetch_key_answered(key_id: Any, host: Any, status: Any, *, caller: str) -> None:
    """After a keyed answer: a status the row lists as "rejected" stops the key."""
    from puffpai.security import keys
    sid = str(key_id or "")
    row = keys.spec(sid)
    if not isinstance(row, dict):
        return
    target = _ascii_host(host)
    try:
        code = int(status)
    except (TypeError, ValueError):
        code = 0
    rejected = ((row.get("check") or {}).get("rejected") or [])
    if code in rejected:
        keys._mark_rejected(sid)  # noqa: SLF001
        keys._audit("credential_use", caller, "rejected", f"{sid} {target} {code // 100}xx")  # noqa: SLF001
    else:
        keys._audit("credential_use", caller, "done", f"{sid} {target} {code // 100}xx")  # noqa: SLF001


class KeyedAnswer:
    """What :func:`keyed_first_hop` hands back: a status, the answer headers
    and at most the bytes the caller allowed, every echo of the key removed.
    It holds no credential. ``raw_length`` is how many bytes came off the
    wire before the echo was removed (for the caller's size rule)."""

    __slots__ = ("status", "headers", "raw_length", "_body")

    def __init__(self, status: int, headers: dict, body: bytes, raw_length: int):
        self.status = status
        self.headers = headers
        self.raw_length = raw_length
        self._body = body

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            out, self._body = self._body, b""
        else:
            out, self._body = self._body[:n], self._body[n:]
        return out

    def close(self) -> None:
        self._body = b""


def _keyed_wire(url: str, headers: dict, timeout: float) -> Any:
    """The one network call for a keyed request: pinned (every address the
    name resolves to must be public, and the socket goes to the address that
    was checked while TLS checks the name), no redirects, no proxy, no
    cookies. Never a caller-supplied transport."""
    import urllib.error
    import urllib.request
    from puffpai.security import pinned_net

    class _NoFollow(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = pinned_net.urllib_opener(pinned_net.Pin(), _NoFollow)
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return exc


def _keyed_url_ok(url: Any) -> bool:
    """Printable ASCII with no space (as ``source_fetch.url_chars_ok``): the
    HTTP library raises on anything else, and CR/LF could split a request."""
    return isinstance(url, str) and bool(url) and url.isascii() \
        and all(32 < ord(ch) < 127 for ch in url)


def _pin_refused_code(exc: BaseException) -> str:
    """The pinned connection's refusal word inside *exc*, or ''."""
    from puffpai.security.egress_ledger import REASON_CODES
    from puffpai.security.pinned_net import PinRefused
    for candidate in (exc, getattr(exc, "reason", None), exc.__cause__,
                      exc.__context__):
        if isinstance(candidate, PinRefused):
            code = getattr(candidate, "code", "")
            return code if code in REASON_CODES and code else "inside_address"
    return ""


def keyed_first_hop(key_id: Any, url: str, headers: Mapping, *, service: str,
                    max_bytes: int, allow_truncated: bool = False,
                    timeout: float) -> "tuple[Optional[KeyedAnswer], str]":
    """Send ONE GET to *url* carrying the sign-up key *key_id*.

    Returns ``(answer, "ok")`` or ``(None, code)`` with *code* one word from
    ``egress_ledger.REASON_CODES`` (``switched_off``, ``not_allowed``,
    ``sign_in_refused``, ``key_host``, ``key_unavailable``,
    ``inside_address``, ``no_answer``, ``bad_answer``, ``too_big``).

    Checked and written HERE, whoever the caller is: the owner's switch for
    *service* (a known service, else refused), and exactly one
    outside-traffic line for this hop, host only, whatever the outcome. The
    answer never holds the key. The body is at most ``max_bytes + 1`` bytes;
    a longer one is refused (``too_big``) unless *allow_truncated*. A
    redirect answer carries its ``Location`` and no body; the caller follows
    it WITHOUT a key (and logs that next hop itself).

    Only the closure ``source_fetch.fetch_small`` builds for one request may
    call it; any other caller gets ``PermissionError``.
    """
    frame = sys._getframe(1)
    where = (frame.f_globals.get("__name__", ""), frame.f_code.co_qualname)
    frame = None
    if where not in _KEYED_HOP_CALLERS:
        raise PermissionError(
            "keyed_first_hop may only be called by source_fetch.fetch_small "
            "or source_fetch.get_archive; "
            f"{where[0]!r} must use that instead.")
    from puffpai.security import egress_ledger, outside_switches
    svc = service if isinstance(service, str) else ""
    what = egress_ledger.FETCH_SERVICES.get(svc)
    target = egress_ledger.safe_host(url) if isinstance(url, str) else ""

    def _line(outcome: str, **fields: Any) -> None:
        egress_ledger.record(svc if what else _KEYED_UNKNOWN_SERVICE,
                             host=target, outcome=outcome,
                             what=what or "request", **fields)

    def _refuse(code: str) -> "tuple[None, str]":
        _line("refused", code=code, bytes_in=0, bytes_out=0)
        return None, code

    if what is None:
        return _refuse("not_allowed")
    if outside_switches.is_off(svc):
        return _refuse("switched_off")
    if not _keyed_url_ok(url):
        return _refuse("not_allowed")
    if not isinstance(headers, Mapping) \
            or any(_is_credential_header(n) for n in headers):
        return _refuse("sign_in_refused")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) \
            or max_bytes <= 0:
        return _refuse("not_allowed")
    limit = min(max_bytes + 1, _KEYED_MAX_READ)
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return _refuse("key_host")
    cred, code, echoes = _fetch_key_credential(key_id, host, caller=svc)
    if cred is None:
        return _refuse(code)
    try:
        extra = _credential_header_for(cred, url)
        if extra is None:
            return _refuse("key_host")
        send = {str(k): str(v) for k, v in headers.items()}
        send[extra[0]] = extra[1]
        extra = None
        sent = len(url) + sum(len(k) + len(v) + 4 for k, v in send.items()) + 16
        try:
            resp = _keyed_wire(url, send, timeout)
        except OSError as exc:
            refused = _pin_refused_code(exc)
            if refused:
                return _refuse(refused)
            _line("failed", code="no_answer", bytes_out=sent)
            return None, "no_answer"
        except (http.client.HTTPException, ValueError):
            _line("failed", code="bad_answer", bytes_out=sent)
            return None, "bad_answer"
        finally:
            send = {}
        raw = b""
        failure = ""
        status = 0
        try:
            for attr in ("status", "code"):
                value = getattr(resp, attr, None)
                if isinstance(value, int) and not isinstance(value, bool):
                    status = value
                    break
            answer_headers: dict[str, str] = {}
            declared = ""
            try:
                for name, value in list(getattr(resp, "headers", {}).items()):
                    text = _credential_scrub(echoes, str(value).encode(
                        "utf-8", "replace")).decode("utf-8", "replace")
                    answer_headers.setdefault(str(name), text)
                    if str(name).strip().lower() == "content-length" and not declared:
                        declared = text.strip()
            except Exception:
                answer_headers = {}
            if status in _KEYED_REDIRECTS:
                pass
            elif not allow_truncated and declared.isdigit() \
                    and int(declared) > max_bytes:
                failure = "too_big"
            else:
                try:
                    got = resp.read(limit)
                except (OSError, http.client.HTTPException, ValueError):
                    failure = "no_answer"
                else:
                    raw = bytes(got) if isinstance(got, (bytes, bytearray)) else b""
        finally:
            try:
                resp.close()
            except Exception:
                pass
            resp = None
        _fetch_key_answered(key_id, host, status, caller=svc)
        if failure == "no_answer":
            _line("failed", code="no_answer", status=status, bytes_out=sent)
            return None, "no_answer"
        if failure == "too_big" or (len(raw) > max_bytes and not allow_truncated):
            _line("refused", code="too_big", status=status, bytes_out=sent,
                  bytes_in=len(raw))
            return None, "too_big"
        _line("done", status=status, bytes_out=sent,
              bytes_in=min(len(raw), max_bytes))
        body = _credential_scrub(echoes, raw)
        return KeyedAnswer(status, answer_headers, body, len(raw)), "ok"
    finally:
        cred = None
        echoes = ()


# ── before the bot is set up: one check of a pasted key ─────────────────────
#
# The wizard asks about GitHub early, before it has recorded
# what the finished bot may reach, so the door cannot open yet (no run
# setting). The build-time channel (fetch_small: the owner's switch and the
# Bridge, no run setting) is what the wizard's own searches use. This is the
# one keyed request that may go out on it with a value not yet stored: the
# curated row's own test address, once, for a row marked
# ``check_before_setup``, under its ``setup_service`` switch, which the owner's yes
# has just switched on. Same rules as the keyed hop: header only, bound to
# that one host, pinned, no redirect, one outside-traffic line, the answer
# discarded (only the status comes back). Only ``puffpai.security.keys`` may
# call it.

def setup_check_hop(key_id: Any, value: str, *, timeout: float = 8.0
                    ) -> "tuple[Optional[int], str, dict]":
    """``(status, "ok", answer)`` or ``(None, code, {})`` for ONE test GET of
    a pasted key before the bot is set up. *answer* is ``{"user": {login,
    name, avatar_url}, "headers": {x-oauth-scopes}}`` from a 200 (the key's
    echo removed), else {}. Refused unless the wizard run says setup is under
    way (``github_account.before_setup``: a positive signal, fail closed)."""
    caller = sys._getframe(1).f_globals.get("__name__", "")
    if caller != _SECURITY_PACKAGE + ".keys":
        raise PermissionError("setup_check_hop may only be called by puffpai.security.keys")
    from puffpai import USER_AGENT
    from puffpai.security import egress_ledger, keys, outside_switches
    row = keys.spec(key_id)
    svc = str((row or {}).get("setup_service") or "")
    what = egress_ledger.FETCH_SERVICES.get(svc)
    url = str(((row or {}).get("check") or {}).get("url") or "")
    target = egress_ledger.safe_host(url) if url else ""

    def _line(outcome: str, **fields: Any) -> None:
        egress_ledger.record(svc if what else _KEYED_UNKNOWN_SERVICE, host=target,
                             outcome=outcome, what=what or "request", **fields)

    if not isinstance(row, dict) or row.get("check_before_setup") is not True or what is None:
        _line("refused", code="not_allowed", bytes_in=0, bytes_out=0)
        return None, "not_allowed", {}
    try:
        from puffpai import github_account
        under_way = github_account.before_setup()
    except Exception:  # noqa: BLE001 - not knowing is "set up": refused
        under_way = False
    if not under_way:
        _line("refused", code="bot_offline", bytes_in=0, bytes_out=0)
        return None, "bot_offline", {}
    if outside_switches.is_off(svc):
        _line("refused", code="switched_off", bytes_in=0, bytes_out=0)
        return None, "switched_off", {}
    try:
        host = _ascii_host(urlsplit(url).hostname or "")
    except ValueError:
        host = ""
    row_hosts = {_ascii_host(h) for h in row.get("hosts") or [] if isinstance(h, str)}
    fmt = (row.get("placement") or {}).get("value_format", "{key}")
    if not host or host not in row_hosts or not _keyed_url_ok(url) \
            or not _value_format_ok(fmt) or not isinstance(value, str) or not value:
        _line("refused", code="key_host", bytes_in=0, bytes_out=0)
        return None, "key_host", {}
    cred = _bind_credential(str((row.get("placement") or {}).get("name") or ""),
                            fmt.replace("{key}", value), [host])
    try:
        extra = _credential_header_for(cred, url)
        if extra is None:
            _line("refused", code="key_host", bytes_in=0, bytes_out=0)
            return None, "key_host", {}
        echoes = (value.encode("utf-8"), extra[1].encode("utf-8"))
        send = {"User-Agent": USER_AGENT, "Accept": "application/json",
                "Accept-Encoding": "identity", extra[0]: extra[1]}
        extra = None
        sent = len(url) + sum(len(k) + len(v) + 4 for k, v in send.items()) + 16
        try:
            resp = _keyed_wire(url, send, timeout)
        except OSError as exc:
            refused = _pin_refused_code(exc)
            if refused:
                _line("refused", code=refused, bytes_in=0, bytes_out=0)
                return None, refused, {}
            _line("failed", code="no_answer", bytes_out=sent)
            return None, "no_answer", {}
        except (http.client.HTTPException, ValueError):
            _line("failed", code="bad_answer", bytes_out=sent)
            return None, "bad_answer", {}
        finally:
            send = {}
        status = 0
        got = 0
        body = b""
        scopes = ""
        try:
            for attr in ("status", "code"):
                v = getattr(resp, attr, None)
                if isinstance(v, int) and not isinstance(v, bool):
                    status = v
                    break
            try:
                for name, hv in list(getattr(resp, "headers", {}).items()):
                    if str(name).strip().lower() == "x-oauth-scopes":
                        scopes = str(hv)[:400]
            except Exception:
                scopes = ""
            try:
                body = bytes(resp.read(64 * 1024) or b"")
                got = len(body)
            except (OSError, http.client.HTTPException, ValueError):
                got, body = 0, b""
        finally:
            try:
                resp.close()
            except Exception:
                pass
            resp = None
        _line("done", status=status, bytes_out=sent, bytes_in=got)
        answer: dict = {}
        if status == 200:
            try:
                data = json.loads(_credential_scrub(echoes, body).decode("utf-8", "replace"))
            except ValueError:
                data = None
            if isinstance(data, dict):
                answer = {"user": {k: str(data.get(k) or "")[:300]
                                   for k in ("login", "name", "avatar_url")},
                          "headers": ({"x-oauth-scopes": scopes} if scopes else {})}
        body = b""
        echoes = ()
        return status, "ok", answer
    finally:
        cred = None


# ── the one network call, and its log line ───────────────────────────────────

def _new_pin(resolver=None):
    from puffpai.security import pinned_net
    return pinned_net.Pin(resolver=resolver)


def pin_add(pin, host: str, answers) -> None:
    """Pin *host* to answers :func:`_check_resolved_public` already vetted."""
    from puffpai.security import pinned_net
    fresh = pinned_net.Pin(preset={host: list(answers)})
    with pin._lock:  # noqa: SLF001 - one pin per request, same package
        pin._pinned.update(fresh._pinned)  # noqa: SLF001


def _send(method: str, url: str, *, pin, **kwargs):
    """THE network call for every door, card, picture, town lookup and sample.

    A pinned ``requests`` session (:func:`pinned_net.requests_session`):
    https only, no proxy from the environment, and the socket goes to the
    address *pin* holds for the URL's host. Tests replace this one function.
    """
    from puffpai.security import pinned_net
    session = pinned_net.requests_session(pin)
    try:
        return session.request(method, url, **kwargs)
    finally:
        session.close()


#: Door id -> what the request was for (``egress_ledger.WHAT`` key).
_WHAT_BY_DOOR = {"weather": "forecast", "air_quality": "air_quality",
                 "news_rss": "headlines", "web_fetch": "web_page",
                 "geocode": "town_lookup",
                 # searches (and a key's one test search)
                 "web_search": "web_search",
                 "brave_search": "web_search",
                 "marginalia_search": "web_search",
                 # the owner's own GitHub account (the sign-in check, the
                 # device sign-in)
                 "github": "github_account",
                 # Toolsmith's cloud model (never the key, never the words)
                 "toolsmith_cloud": "toolsmith_install"}

#: Doors whose address carries no owner value in its path: the path is kept
#: in the log. Every other exit logs the host only.
_PATH_KEPT = frozenset({"weather", "air_quality", "geocode"})


def reason_code(exc: BaseException) -> str:
    """The closed-list reason word for a refusal (``egress_ledger``)."""
    code = str(getattr(exc, "code", "") or "")
    if code:
        return code
    return _code_from_text(str(getattr(exc, "reason", "") or exc))


def _code_from_text(reason: str) -> str:
    """The closed-list reason word for a refusal's reason text."""
    text = str(reason or "").lower()
    if "the bridge" in text:
        return "bridge_up"
    if "no egress door named" in text or "not declared" in text:
        return "not_on_list"
    if "forces every app door shut" in text or "=off" in text:
        return "turned_off_here"
    if "posture:" in text and "is shut" in text:
        return ("switched_off" if f"posture: {OPEN_POSTURE}" in text
                else "bot_offline")
    if "is shut" in text or "switched" in text:
        return "switched_off"
    if "https" in text or "scheme" in text:
        return "not_encrypted"
    if "credential" in text or "auth" in text or "cookie" in text:
        return "sign_in_refused"
    if "landed on" in text:
        return "peer_moved"
    if "cannot resolve" in text or "resolves to nothing" in text \
            or "look up" in text:
        return "cannot_look_up"
    if "private" in text or "loopback" in text or "resolves to" in text \
            or "points at" in text:
        return "inside_address"
    if "more than" in text and "redirect" in text:
        return "too_many_redirects"
    if "redirect" in text:
        return "redirected_away"
    return "not_allowed"


def _ledger_service(door_id: str) -> str:
    """The outside-services id a door's traffic is logged under."""
    app = app_of(door_id) if _is_feed_door(door_id) else None
    if app:
        return f"card-{app}"
    return str(door_id or "").strip().lower() or "unknown"


def _bytes_out(method: str, url: str, headers: Any, body: Any) -> Optional[int]:
    """About how many bytes the request put on the wire (line + headers + body).

    A length only: nothing of the request's content is kept.
    """
    try:
        parts = urlsplit(str(url))
        target = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        total = len(f"{method} {target} HTTP/1.1\r\n".encode("utf-8"))
        total += len(f"Host: {parts.hostname or ''}\r\n".encode("utf-8"))
        if isinstance(headers, Mapping):
            for key, value in headers.items():
                total += len(f"{key}: {value}\r\n".encode("utf-8", "replace"))
        if isinstance(body, (bytes, bytearray)):
            total += len(body)
        elif isinstance(body, str):
            total += len(body.encode("utf-8", "replace"))
        return total + 2
    except Exception:
        return None


def _bytes_in(resp: Any, streamed: bool) -> Optional[int]:
    """The reply's size when it is known without consuming a stream."""
    if not streamed:
        if hasattr(resp, "_content"):
            content = getattr(resp, "_content", None)
        else:
            content = getattr(resp, "content", None)
        if isinstance(content, (bytes, bytearray)):
            return len(content)
    try:
        declared = (getattr(resp, "headers", None) or {}).get("Content-Length")
        return int(declared) if declared is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


class _LedgerTag:
    """Writes the outside-traffic log line for one :func:`egress_request`."""

    def __init__(self, service: str, what: str, keep_path: bool,
                 picture_host: str = ""):
        self.service = service
        self.what = what
        self.keep_path = keep_path
        self.picture_host = picture_host

    @classmethod
    def for_door(cls, d: Optional[Door], door_id: str, url: str) -> "_LedgerTag":
        key = str(door_id or "").strip().lower()
        if d is not None and _is_feed_door(d):
            picture = d.picture_hosts[0] if d.picture_hosts else (
                d.hosts[1] if len(d.hosts) > 1 else "")
            return cls(_ledger_service(key), "card_data", False, picture)
        return cls(_ledger_service(key), _WHAT_BY_DOOR.get(key, "request"),
                   key in _PATH_KEPT)

    def _what_for(self, url: str) -> str:
        try:
            host = _clean_host(urlsplit(str(url)).hostname or "")
        except ValueError:
            host = ""
        if self.picture_host and host == self.picture_host:
            return "card_picture"
        return self.what

    def _path(self, url: str) -> str:
        if not self.keep_path:
            return ""
        from puffpai.security import egress_ledger
        return egress_ledger.safe_path(url)

    def refused(self, blocked: BaseException, url: str, method: str) -> None:
        from puffpai.security import egress_ledger
        egress_ledger.record(
            self.service, host=egress_ledger.safe_host(url),
            outcome="refused", method=method, path=self._path(url),
            code=reason_code(blocked), what=self._what_for(url),
            bytes_out=0, bytes_in=0)

    def send(self, pin, d: Door, method: str, url: str, **kwargs):
        """Send one hop through :func:`_send`, log it, and return the reply."""
        from puffpai.security import egress_ledger
        host = egress_ledger.safe_host(url)
        out = _bytes_out(method, url, kwargs.get("headers"),
                         kwargs.get("data") or kwargs.get("json"))
        try:
            resp = _send(method, url, pin=pin, **kwargs)
        except Exception as exc:
            refused = getattr(pin, "refused", None)
            if refused is not None:
                blocked = EgressBlocked(
                    d.id, f"pinned connection refused: {refused.reason}",
                    "That request tried to reach an address on this machine "
                    "or this network, so nothing was sent.",
                    code=getattr(refused, "code", "inside_address"))
                self.refused(blocked, url, method)
                raise blocked from None
            egress_ledger.record(
                self.service, host=host, outcome="failed", method=method,
                path=self._path(url), code="no_answer",
                what=self._what_for(url), bytes_out=out)
            raise exc
        req = getattr(resp, "request", None)
        if req is not None and isinstance(getattr(req, "headers", None), Mapping):
            out = _bytes_out(method, url, req.headers,
                             getattr(req, "body", None)) or out
        egress_ledger.record(
            self.service, host=host, outcome="done", method=method,
            path=self._path(url), status=getattr(resp, "status_code", None),
            bytes_in=_bytes_in(resp, bool(kwargs.get("stream"))),
            bytes_out=out, what=self._what_for(url))
        return resp


# ── pictures only ───────────────────────────────────────────────────────────

def _picture_request_ok(d: "Door", method: str, kwargs: dict, credential: Any) -> None:
    """A pictures-only host gets a plain GET: no query of ours, no body, no
    sign-in (headers were already screened for anything credential-shaped)."""
    if str(method).upper() != "GET" or credential is not None \
            or any(k in kwargs for k in ("params", "data", "json", "files")):
        raise EgressBlocked(d.id, "a pictures-only site gets a plain GET only",
                            "That site is reached for pictures only, so nothing was sent.",
                            code="not_on_list")


class _PictureAnswer:
    """A picture answer read in full (capped), standing in for the response."""

    def __init__(self, status: int, headers: dict, body: bytes):
        self.status_code, self.headers, self.content = status, headers, body
        self.ok = 200 <= status < 300

    def iter_content(self, chunk_size: int = 65536, *_a, **_k):
        size = max(1, int(chunk_size or 65536))
        for i in range(0, len(self.content), size):
            yield self.content[i:i + size]

    def close(self) -> None:
        pass


def _picture_answer(d: "Door", resp: Any,
                    max_bytes: Optional[int] = None) -> "_PictureAnswer":
    """Only a picture comes back from a pictures-only site: an image type
    (never SVG), at most :data:`RESULT_PICTURE_MAX_BYTES`. Anything else is
    refused (:class:`EgressBlocked`, code ``not_a_picture`` / ``too_big``)."""
    cap = int(max_bytes or RESULT_PICTURE_MAX_BYTES)
    try:
        status = int(getattr(resp, "status_code", 0) or 0)
        headers = {str(k).lower(): str(v) for k, v in
                   dict(getattr(resp, "headers", {}) or {}).items()}
        ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if status == 200 and ctype not in RESULT_PICTURE_TYPES:
            raise EgressBlocked(d.id, f"answer is {ctype or 'untyped'}",
                                "That address did not answer with a picture.",
                                code="not_a_picture")
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > cap:
            raise EgressBlocked(d.id, "picture too big", "That picture was too big.",
                                code="too_big")
        chunks, total = [], 0
        it = getattr(resp, "iter_content", None)
        for chunk in (it(65536) if callable(it) else [getattr(resp, "content", b"") or b""]):
            total += len(chunk or b"")
            if total > cap:
                raise EgressBlocked(d.id, "picture too big", "That picture was too big.",
                                    code="too_big")
            chunks.append(bytes(chunk or b""))
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            close()
    keep = {k: v for k, v in headers.items() if k in ("content-type", "cache-control",
                                                      "last-modified", "etag")}
    out = {"Content-Type": keep.pop("content-type", "")}
    out.update(keep)
    return _PictureAnswer(status, out, b"".join(chunks))


# ── pictures a source's own results point to ─────────────────────────────────
#
# A data source's switch row says "<source> and the picture sites its results
# point to" (build.setup); the owner's yes to it covers both. The picture sites are
# not known before the owner's yes (no look may run before it), so they are not a
# host list: a picture is let through ONLY when the source's own door is open
# (that yes, and the run posture) AND its host appeared in that source's own
# results (the caller keeps those, build: assemble.server.host). GET only, no
# query of the owner's, no cookie or key, image types only (never SVG), size-capped,
# same public-address pinning and same-host redirects as any dynamic door.

_SCOPE_KEY = object()


class _PictureScope:
    """Made only by :func:`result_picture_get`; anything else is refused."""
    __slots__ = ("source_door", "hosts", "_key")

    def __init__(self, source_door: str, hosts: frozenset, key: object):
        self.source_door, self.hosts, self._key = source_door, hosts, key


def _picture_scope_open(door_id: Any, method: str, url: str, scope: Any, kwargs: dict,
                        credential: Any) -> tuple[Door, str]:
    if str(door_id) != RESULT_PICTURES or not isinstance(scope, _PictureScope) \
            or scope._key is not _SCOPE_KEY:  # noqa: SLF001 - same module
        raise EgressBlocked(str(door_id), "picture door without its scope",
                            "That picture was not asked for the usual way, so nothing "
                            "was fetched.", code="not_on_list")
    if str(method).upper() != "GET" or credential is not None or set(kwargs) - {"stream"}:
        raise EgressBlocked(RESULT_PICTURES, "picture request carries more than a GET",
                            "Only a plain picture request is allowed there.",
                            code="not_on_list")
    off = _as_bool(os.environ.get(door(RESULT_PICTURES).env_var) or "")
    if off is False:
        raise EgressBlocked(RESULT_PICTURES, "PUFFPAI_EGRESS_RESULT_PICTURES=off",
                            door(RESULT_PICTURES).settings_hint, code="turned_off_here")
    require_open(scope.source_door)          # the owner's yes to the source
    d = door(RESULT_PICTURES)
    host = _dynamic_check_url(d, url)
    if host not in scope.hosts:
        raise EgressBlocked(RESULT_PICTURES, f"{host!r} is not in the source's results",
                            "That picture's site was not in the source's own results, so "
                            "nothing was fetched.", code="not_on_list")
    return d, host


_PICTURE_AUDITED: set[tuple[str, str]] = set()


def result_picture_get(source_door: str, url: str, result_hosts: Any, *,
                       timeout: float = DEFAULT_TIMEOUT,
                       max_bytes: int = RESULT_PICTURE_MAX_BYTES) -> tuple[int, dict, bytes]:
    """One picture from a site *source_door*'s own results pointed to
    (*result_hosts*: hosts seen in them). ``(status, headers, body)``.
    Raises :class:`EgressBlocked` when the source is shut, the host was not
    in its results, or the answer is not a picture or too big."""
    hosts = frozenset(str(h).strip().rstrip(".").lower() for h in result_hosts or ()
                      if isinstance(h, str) and h)
    scope = _PictureScope(str(source_door), hosts, _SCOPE_KEY)
    answer = egress_request(RESULT_PICTURES, "GET", url, timeout=timeout,
                            picture_scope=scope)
    if len(answer.content) > max_bytes:
        raise EgressBlocked(RESULT_PICTURES, "picture too big", "That picture was too big.",
                            code="too_big")
    status = int(answer.status_code)
    headers = {str(k).lower(): str(v) for k, v in answer.headers.items()}
    host = _clean_host(urlsplit(url.strip()).hostname or "")
    if (str(source_door), host) not in _PICTURE_AUDITED:
        _PICTURE_AUDITED.add((str(source_door), host))
        try:
            from puffpai.security.audit_log import AuditLog
            AuditLog().log(action="result_picture", user="owner", result="fetched",
                           details=f"{str(source_door)[:80]} -> {host}"[:200])
        except Exception:  # noqa: BLE001 - the ledger line is kept anyway
            pass
    return status, {k: v for k, v in headers.items() if k in ("content-type",)}, \
        answer.content


def _picture_request(door_id: Any, method: str, url: str, scope: Any,
                     kwargs: dict, credential: Any, *, timeout: float,
                     resolver=None) -> "_PictureAnswer":
    """The one path through :data:`RESULT_PICTURES`: a plain GET to a host a
    source's own results named, pinned to its checked address, same-host
    redirects only, answered by a size-capped image."""
    tag = _LedgerTag.for_door(door(RESULT_PICTURES), str(door_id), url)
    try:
        d, origin_host = _picture_scope_open(door_id, method, url, scope, kwargs,
                                             credential)
        pin = _new_pin(resolver)
        pin_add(pin, origin_host,
                _check_resolved_public(d, origin_host, _port_of(url), resolver))
    except EgressBlocked as blocked:
        tag.refused(blocked, url, method)
        raise
    current, current_host = url, origin_host
    for _hop in range(MAX_REDIRECTS + 1):
        resp = tag.send(pin, d, "GET", current, timeout=timeout,
                        allow_redirects=False, stream=True)
        _verify_peer(d, resp, current_host)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location") or ""
            _close_quietly(resp)
            nxt = urljoin(current, location)
            try:
                nxt_host = _dynamic_check_url(d, nxt, hop_host=origin_host)
                pin_add(pin, nxt_host,
                        _check_resolved_public(d, nxt_host, _port_of(nxt),
                                               resolver))
            except EgressBlocked as blocked:
                tag.refused(blocked, nxt, method)
                raise
            current, current_host = nxt, nxt_host
            continue
        return _picture_answer(d, resp)
    blocked = EgressBlocked(
        d.id, f"more than {MAX_REDIRECTS} redirects",
        "That site redirected too many times, so nothing was fetched.",
        code="too_many_redirects")
    tag.refused(blocked, current, method)
    raise blocked


# ── a browser app's outside sites, on the app door ───────────────────────────
#
# A browser app Toolsmith added (``puffpai.webapps``) runs in its own locked
# frame with no internet. When the install-time scan finds it needs an outside
# site (its data service, its maps), the app's forwarding address sends the
# request here, and ONLY here: :func:`app_web_request`.
#
# Built on the app feed door above, with the same rules made narrower:
#
# 1. **Registered, bounded.** :func:`register_web_app_door` gives the app one
#    door, ``app:<app id>:web``, naming at most :data:`MAX_FEED_DOOR_HOSTS`
#    public names (each through :func:`is_registrable_hostname`). Web doors
#    live in their own table: :func:`door` and :func:`egress_request` never
#    see them, so no other path (and no config key or per-door environment
#    value) can open one.
# 2. **Each site is its own yes.** A site is off until the owner switches it
#    on with an owner-only consent ticket (``app_host_on``, bound to this app
#    and this site). The recorded yes lives in ``egress_app_doors.json`` under
#    a flag bound to the door's WHOLE site set, so an app that now names a new
#    site has every site off again until the owner says yes anew. Switching a
#    site off never needs a ticket. ``PUFFPAI_EGRESS_APPS=off`` forces every
#    site off. With the Bridge sealed nothing goes out at all.
# 3. **Narrow requests.** https on 443, no sign-in in the address, every
#    address the name resolves to public, and the socket pinned to the
#    address that was checked. Request headers: only ``content-type``,
#    ``accept``, ``accept-language`` (others are dropped, and only their
#    names are logged). A sign-up key rides only when the caller names its
#    handle, only on the first hop, bound to exactly that site, and every echo
#    of it is removed from the answer. At most two redirects, each only to a
#    site of this app that is switched on; a redirect never carries the key.
# 4. **Narrow answers.** Only ``content-type``, ``content-length``,
#    ``cache-control``, ``etag`` and ``last-modified`` come back (never
#    ``Set-Cookie``, a CSP, ``Clear-Site-Data``, ``Service-Worker-Allowed`` or
#    ``Location``), and sizes are capped both ways.
# 5. **Logged.** One outside-traffic line per request on the wire (and one per
#    refusal), host only, ``what="app_traffic"``, service ``app:<id>:web``.

def _bot_offline() -> bool:
    """True when the Bridge lets nothing reach the internet."""
    return _bridge_level() == "sealed"


#: The web door id's last part.
WEB_DOOR_SUFFIX = ":web"

#: A browser app's id as ``integrate.records`` issues it (``webapps.ID_RE``):
#: re-stated, not imported, like :data:`_APP_ID_RE`. At most 39 characters.
_WEB_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,29}-[a-z2-7]{8}\Z")

#: Methods a browser app may forward. Anything else is refused.
WEB_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})

#: Request headers an app may send (lower-case). Every other one is dropped.
WEB_REQUEST_HEADERS = ("content-type", "accept", "accept-language")

#: Answer headers an app gets back (lower-case). Every other one is dropped.
WEB_ANSWER_HEADERS = ("content-type", "content-length", "cache-control", "etag",
                      "last-modified")

#: The refusal words :func:`app_web_request` answers with (``{"refused": code}``).
WEB_REFUSALS = frozenset({
    "switched_off", "not_https", "not_443", "userinfo", "inside_address",
    "too_big", "redirect_off_list", "unknown_app",
    # also honest, beyond the pinned list: the bot is set to stay off the
    # internet, a method or size the app may not use, no DNS answer, a third
    # redirect, a key that cannot be used or not for this site, no answer.
    "bot_offline", "not_allowed", "cannot_look_up", "redirects",
    "key_unavailable", "key_host", "no_answer", "bad_answer", "peer_moved",
    # a DNS filter at home (Pi-hole, AdGuard Home) blocked the name.
    "network_filter"})

WEB_MAX_REDIRECTS = 2
WEB_TIMEOUT = 15.0
_WEB_MAX_OUT_CAP = 10 * 1024 * 1024
_WEB_MAX_IN_CAP = 50 * 1024 * 1024
_WEB_MAX_HEADER_VALUE = 512
_WEB_WHAT = "app_traffic"
#: The log's service id for a request naming no web door this build has.
_WEB_UNKNOWN_SERVICE = "app_web"
#: Tests replace this; the running app looks names up with the system.
_WEB_RESOLVER = None

#: The pinned refusal word -> the outside-traffic log's own word (so the
#: "What leaves your computer" page has words for every line).
_WEB_LEDGER_CODE = {
    "unknown_app": "not_on_list", "not_https": "not_encrypted",
    "not_443": "not_allowed", "userinfo": "sign_in_refused",
    "redirect_off_list": "redirected_away",
}

_WEB_DOORS: dict[str, Door] = {}


def web_door_id(app_id: str) -> str:
    """``app:<app id>:web``."""
    return f"{APP_DOOR_PREFIX}{app_id}{WEB_DOOR_SUFFIX}"


def _web_flag(app_id: str, hosts) -> str:
    """The recorded-yes key: this app and its WHOLE site set."""
    return f"app_{app_id}_web_{_host_digest(sorted(set(hosts)))}"


def _web_host(host: Any) -> str:
    return str(host).strip().rstrip(".").lower() if isinstance(host, str) else ""


def register_web_app_door(app_id: str, hosts: list[str]) -> str:
    """Give browser app *app_id* its door for *hosts*; returns its id.

    Raises ``ValueError`` for an id that is not a browser app's, a name that
    is not a public registrable one, or more than :data:`MAX_WEB_APP_HOSTS`
    sites. Registering again replaces the door; a different site set means a
    different recorded-yes key, so every site starts off again.
    """
    if not isinstance(app_id, str) or not _WEB_APP_ID_RE.match(app_id):
        raise ValueError("not a browser app's id")
    if isinstance(hosts, (str, bytes)) or not isinstance(hosts, (list, tuple)):
        raise ValueError("hosts must be a list of site names")
    clean: list[str] = []
    for raw in hosts:
        host = _web_host(raw)
        if not is_registrable_hostname(host):
            raise ValueError("a site is not a public name")
        if host not in clean:
            clean.append(host)
    if len(clean) > MAX_WEB_APP_HOSTS:
        raise ValueError(f"at most {MAX_WEB_APP_HOSTS} outside sites per app")
    flag = _web_flag(app_id, clean)
    d = Door(
        id=web_door_id(app_id), hosts=tuple(clean),
        purpose="An added app's outside sites.",
        data_sent="What the app sends there, and your internet address.",
        config_flag=flag,
        # Never read for a web door: a site opens only with the owner's
        # recorded yes. Only the operator's "off" (APPS_KILL_ENV) is honoured.
        env_var=APPS_KILL_ENV,
        consent_kind="owner_flag",
        enforcement="registry",
        probe_ok=False,
        settings_hint=("That outside site is switched off for this app, so "
                       "nothing was sent."),
        default=False,
        notes=("A browser app's outside sites. Each is off until the owner "
               "says yes to it; only app_web_request uses this door."))
    with _APP_LOCK:
        _WEB_DOORS[app_id] = d
    logger.info("egress: browser app %s may ask for %d outside site(s), all "
                "off until the owner says yes", _shown(app_id), len(clean))
    return d.id


def unregister_web_app_door(app_id: str) -> bool:
    """Forget *app_id*'s door and every yes recorded for it (app removed)."""
    key = app_id if isinstance(app_id, str) else ""
    with _APP_LOCK:
        gone = _WEB_DOORS.pop(key, None)
    if key:
        with _web_set_lock:
            _store_web_consent(key, None)
    return gone is not None


def web_app_door(app_id: str) -> Optional[Door]:
    """The web door for *app_id*, or None."""
    with _APP_LOCK:
        return _WEB_DOORS.get(app_id if isinstance(app_id, str) else "")


def web_app_ids() -> tuple[str, ...]:
    """Every browser app with a web door, in registration order."""
    with _APP_LOCK:
        return tuple(_WEB_DOORS)


def clear_web_app_doors() -> None:
    """Forget every web door (tests). Recorded yeses stay on disk."""
    with _APP_LOCK:
        _WEB_DOORS.clear()


def _web_on_in(entry: Any, d: Door) -> set[str]:
    """The sites of *d* a recorded entry has on (fails to none)."""
    if not isinstance(entry, dict) or entry.get("kind") != "web" \
            or entry.get("open") is not True:
        return set()
    listed = entry.get("hosts_on")
    if not isinstance(listed, list):
        return set()
    return {h for h in listed if isinstance(h, str) and h in d.hosts}


def _recorded_web_on(d: Door) -> set[str]:
    """The sites of *d* the owner switched on (the file only; fails to none)."""
    return _web_on_in(_read_app_consent().get(d.config_flag), d)


def _web_killed() -> bool:
    kill = os.environ.get(APPS_KILL_ENV)
    return kill is not None and _as_bool(kill) is False


def app_web_hosts(app_id: str) -> dict[str, bool]:
    """``{site: on}`` for every site of *app_id*'s door; ``{}`` when it has none.

    On means the owner said yes to that site and this computer's settings do
    not force app sites off.
    """
    d = web_app_door(app_id)
    if d is None:
        return {}
    on = set() if _web_killed() else _recorded_web_on(d)
    return {h: (h in on) for h in d.hosts}


def _by_kind(by: Any) -> str:
    text = str(by or "")
    if text.startswith("device:"):
        return "device"
    if text.startswith("terminal:"):
        return "terminal"
    return text if text in ("owner", "wizard", "installer") else "owner"


#: Held across every change to a browser app's sites: the read of what is
#: on, the spending of the owner's yes, and the write. Without it, a change
#: that read "site A is on" before the owner switched A off could write A back
#: on with no new yes. Re-entrant so a change made from inside another (a
#: nested owner action on this thread) cannot deadlock. Taken BEFORE
#: ``_consent_lock``, never while holding it.
_web_set_lock = threading.RLock()


def _store_web_consent(app_id: str,
                       entry: "Optional[dict] | Callable[[dict], dict]") -> None:
    """Write (or with None, drop every) web yes for *app_id*. Atomic.

    *entry* may be a function of the freshly read ``{flag: entry}`` map that
    returns the entry to write: it runs under the file lock, so a change is
    applied to what is on the disk now, not to an older read.

    Raises ``OSError``/``ValueError`` rather than write a yes over a file that
    could not be read.
    """
    from datetime import datetime, timezone
    path = app_consent_path()
    with _consent_lock:
        existing: dict[str, Any] = {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if (isinstance(loaded, dict)
                    and loaded.get("schema") == APP_CONSENT_SCHEMA
                    and isinstance(loaded.get("doors"), dict)):
                existing = {k: v for k, v in loaded["doors"].items()
                            if isinstance(k, str) and isinstance(v, dict)}
        except FileNotFoundError:
            existing = {}
        except (OSError, ValueError):
            # Never write over a file that could not be read: that would
            # lose every other recorded yes (and the evidence). While it
            # cannot be read, every app door and site reads as shut anyway,
            # so a removal has nothing more to do and a change is refused.
            if entry is None:
                return
            raise
        if callable(entry):
            entry = entry(dict(existing))
        # One app has one web door: yeses for its older site sets go too.
        existing = {k: v for k, v in existing.items()
                    if not (v.get("kind") == "web" and v.get("app") == app_id)}
        if entry is not None:
            entry = dict(entry)
            entry.setdefault("at", datetime.now(timezone.utc)
                             .isoformat(timespec="seconds"))
            existing[entry.pop("flag")] = entry
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"schema": APP_CONSENT_SCHEMA, "doors": existing}, fh,
                      indent=2, sort_keys=True)
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover - Windows ignores the mode
            pass
        os.replace(tmp, path)
        _consent_cache.update({"path": None, "mtime": None, "doors": {}})


_WEB_SET_WORDS = {
    "unknown_app": "That app has no outside sites here, so nothing was changed.",
    "not_on_list": "That app did not ask for that site, so nothing was changed.",
    "one_at_a_time": "Say yes to one outside site at a time.",
    "cannot": "Your choice could not be saved on this computer, so nothing "
              "was changed.",
}


def set_app_web_hosts(app_id: str, hosts_on: list[str], *, by: str,
                      ticket: Optional[str] = None) -> dict:
    """Record which of *app_id*'s sites are on: *hosts_on*, never more.

    A site ends up on only if it is on when the change is written or its yes
    is spent in this call, so a site switched off while this call was in
    flight stays off. Switching a site OFF needs nothing. Switching one ON needs *ticket*: an
    owner-only consent ticket (``app_host_on``, target
    ``consent_tickets.target_app_host(app_id, site)``) minted for the device
    *by* names (``device:<id>`` or ``terminal:<id>``), spent here. One site
    can be switched on per call. Opens no socket.

    Returns ``{"ok": True, "door", "hosts": {site: on}, "switched_on",
    "switched_off", "bot_offline"}`` or ``{"ok": False, "refused": code,
    "message": plain words}``.
    """
    from puffpai.security import consent_tickets

    def _no(code: str, words: str = "") -> dict:
        return {"ok": False, "refused": code,
                "message": words or _WEB_SET_WORDS.get(code, "")}

    d = web_app_door(app_id)
    if d is None:
        return _no("unknown_app")
    if isinstance(hosts_on, (str, bytes)) \
            or not isinstance(hosts_on, (list, tuple, set, frozenset)):
        return _no("not_on_list")
    want: set[str] = set()
    for raw in hosts_on:
        host = _web_host(raw)
        if host not in d.hosts:
            return _no("not_on_list")
        want.add(host)
    # One lock across read, yes and write, and the write applied to what is
    # on the disk at that moment: a site can end up on only if it is on
    # there now or its yes was spent in this call. So a site switched off
    # while this call was in flight stays off.
    with _web_set_lock:
        current = _recorded_web_on(d)
        new_on = sorted(want - current)
        if len(new_on) > 1:
            return _no("one_at_a_time")
        if new_on:
            target = consent_tickets.target_app_host(app_id, new_on[0])
            ok, verdict = consent_tickets.redeem(ticket, by, "app_host_on", target)
            if not ok:
                return _no(verdict, consent_tickets.refusal_words(verdict))
        done = _write_web_hosts(d, app_id, keep=want, add=set(new_on), by=by)
    if done is None:
        return _no("cannot")
    before, after = done
    on, off = sorted(after - before), sorted(before - after)
    logger.info("egress: browser app %s: %d outside site(s) switched on, %d "
                "off", _shown(app_id), len(on), len(off))
    return {"ok": True, "door": d.id, "hosts": app_web_hosts(app_id),
            "switched_on": on, "switched_off": off,
            "bot_offline": _bot_offline()}


def _write_web_hosts(d: Door, app_id: str, *, keep: set[str], add: set[str],
                     by: str) -> "Optional[tuple[set[str], set[str]]]":
    """Write *d*'s sites as ``(on now & keep) | add``, where "on now" is read
    under the file lock. *add* must only hold sites whose yes was just spent.
    Returns ``(before, after)``, or None when it could not be saved (nothing
    changed). Call with :data:`_web_set_lock` held."""
    seen: dict[str, set[str]] = {}

    def build(existing: dict) -> dict:
        before = _web_on_in(existing.get(d.config_flag), d)
        after = (before & keep) | (add & set(d.hosts))
        seen["before"], seen["after"] = before, after
        return {"flag": d.config_flag, "kind": "web", "app": app_id,
                "open": bool(after), "hosts": list(d.hosts),
                "hosts_on": sorted(after), "by": _by_kind(by)}

    try:
        _store_web_consent(app_id, build)
    except (OSError, ValueError):
        return None
    return seen["before"], seen["after"]


def switch_off_web_hosts(app_id: str, hosts: list[str], *, by: str) -> dict:
    """Switch *hosts* of *app_id* off, and nothing else. Needs no yes.

    Unlike :func:`set_app_web_hosts` this names only the sites to switch
    off, so a site switched on (with its own yes) since the caller last
    looked is left as it is. Sites the app did not ask for are ignored.
    Returns the same shape as :func:`set_app_web_hosts`. Opens no socket.
    """
    d = web_app_door(app_id)
    if d is None:
        return {"ok": False, "refused": "unknown_app",
                "message": _WEB_SET_WORDS["unknown_app"]}
    if isinstance(hosts, (str, bytes)) \
            or not isinstance(hosts, (list, tuple, set, frozenset)):
        return {"ok": False, "refused": "not_on_list",
                "message": _WEB_SET_WORDS["not_on_list"]}
    gone = {_web_host(h) for h in hosts}
    with _web_set_lock:
        done = _write_web_hosts(d, app_id, keep=set(d.hosts) - gone, add=set(),
                                by=by)
    if done is None:
        return {"ok": False, "refused": "cannot",
                "message": _WEB_SET_WORDS["cannot"]}
    before, after = done
    off = sorted(before - after)
    logger.info("egress: browser app %s: %d outside site(s) switched off",
                _shown(app_id), len(off))
    return {"ok": True, "door": d.id, "hosts": app_web_hosts(app_id),
            "switched_on": [], "switched_off": off,
            "bot_offline": _bot_offline()}


def _web_url_code(url: Any) -> "tuple[str, str]":
    """``('', host)`` when *url* has the shape a web request may use, else
    ``(code, host_or_'')``. Opens nothing."""
    if not _keyed_url_ok(url):
        return "not_allowed", ""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "not_allowed", ""
    host = _web_host(parts.hostname or "")
    if (parts.scheme or "").lower() != "https":
        return "not_https", host
    if parts.username is not None or parts.password is not None \
            or "@" in (parts.netloc or ""):
        return "userinfo", host
    if port is not None and port != 443:
        return "not_443", host
    if not host:
        return "not_allowed", ""
    return "", host


def _web_request_headers(headers: Any) -> "tuple[dict[str, str], list[str]]":
    """``(kept, dropped names)``: only :data:`WEB_REQUEST_HEADERS`, values
    printable, short and single-line."""
    kept: dict[str, str] = {}
    dropped: list[str] = []
    if headers is None:
        return kept, dropped
    if not isinstance(headers, Mapping):
        return kept, ["(not a list of headers)"]
    for name, value in headers.items():
        low = name.strip().lower() if isinstance(name, str) else ""
        ok_value = (isinstance(value, str) and len(value) <= _WEB_MAX_HEADER_VALUE
                    and value.isascii()
                    and all(32 <= ord(c) < 127 or c == "\t" for c in value))
        if low in WEB_REQUEST_HEADERS and ok_value and low not in kept:
            kept[low] = value
        else:
            dropped.append(name if isinstance(name, str) else type(name).__name__)
    return ({"-".join(p.capitalize() for p in k.split("-")): v
             for k, v in kept.items()}, dropped)


def _web_header_items(resp: Any) -> list:
    try:
        headers = getattr(resp, "headers", None)
        return list(headers.items()) if headers is not None else []
    except Exception:
        return []


def _web_header(resp: Any, name: str) -> str:
    """One answer header, whatever case the server wrote it in."""
    wanted = name.lower()
    for key, value in _web_header_items(resp):
        if isinstance(key, str) and key.strip().lower() == wanted:
            return str(value)
    return ""


def _web_answer_headers(resp: Any, echoes: "tuple[bytes, ...]") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in _web_header_items(resp):
        low = key.strip().lower() if isinstance(key, str) else ""
        if low not in WEB_ANSWER_HEADERS or low in out:
            continue
        text = str(value)
        if echoes:
            text = _credential_scrub(echoes, text.encode("utf-8", "replace")
                                     ).decode("utf-8", "replace")
        if len(text) > _WEB_MAX_HEADER_VALUE \
                or any(ord(c) < 32 and c != "\t" for c in text):
            continue
        if low == "content-length" and not text.strip().isdigit():
            continue
        out[low] = text
    return out


def _web_wire(method: str, url: str, headers: dict, body: Optional[bytes],
              timeout: float, pin: Any) -> Any:
    """THE network call for a browser app's outside request: pinned to the
    address that was checked, no redirects followed, no proxy, no cookies.
    Tests replace this one function."""
    import urllib.error
    import urllib.request
    from puffpai.security import pinned_net

    class _NoFollow(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    request = urllib.request.Request(url, data=body, headers=headers,
                                     method=method)
    opener = pinned_net.urllib_opener(pin, _NoFollow)
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return exc


def _web_status(resp: Any) -> int:
    for attr in ("status", "code"):
        value = getattr(resp, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return 0


def _web_close(resp: Any) -> None:
    try:
        resp.close()
    except Exception:
        pass


def _web_size(value: Any, cap: int) -> Optional[int]:
    """A caller's size limit, clamped to *cap*; None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return min(value, cap)


# -- a refusal the owner should hear about -------------------------------------
#
# An app that reaches a site it has no place for (no door at all, or a site
# its door does not name) is refused, which on its own leaves its panels
# empty with no word why. The refusal stays exactly as it is; on top of it,
# the site is handed to the build's runtime capture
# (``puffpai.build.make.note_runtime_refusal``), which records ONE
# "Allow <app> to reach <host>" setup row per app, asked once. Nothing here
# opens anything, and a capture that fails never changes the refusal.

#: Tests (and the build layer, if it wants) set this; None means the build
#: layer's own capture, imported when first needed.
WEB_REFUSAL_HOOK: Optional[Callable[[str, str, str], Any]] = None


def _note_web_refusal(app_id: str, url: Any, code: str) -> None:
    """Hand one refusal (app, host, code) to the capture. Never raises."""
    try:
        bad, host = _web_url_code(url)
        if bad or not host or not is_registrable_hostname(host):
            return
        fn = WEB_REFUSAL_HOOK
        if fn is None:
            import importlib
            fn = getattr(importlib.import_module("puffpai.build.make"),
                         "note_runtime_refusal", None)
        if callable(fn):
            fn(app_id, host, code)
    except Exception as exc:  # noqa: BLE001 - the refusal itself already stands
        logger.debug("egress: refusal not handed on (%s)", type(exc).__name__)


def app_web_request(app_id: str, method: str, url: str, *,
                    body: Optional[bytes] = None,
                    headers: Optional[dict] = None,
                    key_handle: Optional[str] = None,
                    max_out: int = 1_000_000, max_in: int = 5_000_000) -> dict:
    """One outside request for browser app *app_id*. Never raises.

    Returns ``{"status", "headers", "body"}`` or ``{"refused": code}`` with
    *code* from :data:`WEB_REFUSALS`. Nothing is sent unless every check
    passes; see the section notes above for the rules.
    """
    from puffpai.security import egress_ledger, keys, pinned_net

    app = app_id if isinstance(app_id, str) else ""
    d = web_app_door(app)
    service = web_door_id(app) if d is not None else _WEB_UNKNOWN_SERVICE
    verb = method.strip().upper() if isinstance(method, str) else ""
    logged_verb = verb if verb in WEB_METHODS else "GET"

    def _line(target: Any, outcome: str, **fields: Any) -> None:
        code = fields.pop("code", "")
        if code:
            fields["code"] = _WEB_LEDGER_CODE.get(code, code)
        egress_ledger.record(service, host=egress_ledger.safe_host(target),
                             outcome=outcome, method=logged_verb,
                             what=_WEB_WHAT, **fields)

    def _refuse(code: str, target: Any = url, *, sent: int = 0) -> dict:
        _line(target, "refused", code=code, bytes_in=0, bytes_out=sent)
        return {"refused": code}

    if d is None:
        _note_web_refusal(app, url, "unknown_app")
        return _refuse("unknown_app")
    if verb not in WEB_METHODS:
        return _refuse("not_allowed")
    cap_out = _web_size(max_out, _WEB_MAX_OUT_CAP)
    cap_in = _web_size(max_in, _WEB_MAX_IN_CAP)
    if cap_out is None or cap_in is None:
        return _refuse("not_allowed")
    code, host = _web_url_code(url)
    if code:
        return _refuse(code)
    if _web_killed():
        _line(url, "refused", code="turned_off_here", bytes_in=0, bytes_out=0)
        return {"refused": "switched_off"}
    if host not in _recorded_web_on(d):
        if host not in d.hosts:
            # A site its door does not name: no switch could ever open it.
            _note_web_refusal(app, url, "not_on_list")
        return _refuse("switched_off")
    if _bot_offline():
        return _refuse("bot_offline")
    if body is not None and not isinstance(body, (bytes, bytearray)):
        return _refuse("not_allowed")
    if body is not None and len(body) == 0:
        body = None
    if body is not None:
        if verb in ("GET", "HEAD"):
            return _refuse("not_allowed")
        if len(body) > cap_out:
            return _refuse("too_big")
        body = bytes(body)
    send, dropped = _web_request_headers(headers)
    if dropped:
        logger.info("browser app %s: request headers not sent: %s",
                    _shown(app), ", ".join(_shown(n) for n in dropped[:10]))
    sid = ""
    cred = None
    echoes: "tuple[bytes, ...]" = ()
    try:
        if key_handle is not None:
            sid = keys.service_of(key_handle)
            if not sid:
                return _refuse("key_unavailable")
            cred, key_code, echoes = _fetch_key_credential(
                sid, host, caller="app_web", only_target=True)
            if cred is None:
                return _refuse(key_code if key_code in WEB_REFUSALS
                               else "key_unavailable")
        current, current_host = url, host
        send_method, send_body = verb, body
        for hop in range(WEB_MAX_REDIRECTS + 1):
            try:
                answers = pinned_net.resolve_public(current_host, 443,
                                                    _WEB_RESOLVER)
            except pinned_net.PinRefused as exc:
                word = exc.code if exc.code in WEB_REFUSALS else "inside_address"
                return _refuse(word, current)
            pin = pinned_net.Pin(preset={current_host: answers})
            wire_headers = {"User-Agent": FEED_USER_AGENT,
                            "Accept-Encoding": "identity", **send}
            keyed = hop == 0 and cred is not None
            if keyed:
                extra = _credential_header_for(cred, current)
                if extra is None:
                    return _refuse("key_host", current)
                wire_headers[extra[0]] = extra[1]
                extra = None
            sent = _bytes_out(send_method, current, wire_headers, send_body) or 0
            try:
                resp = _web_wire(send_method, current, wire_headers, send_body,
                                 WEB_TIMEOUT, pin)
            except OSError as exc:
                refused = _pin_refused_code(exc)
                if refused:
                    return _refuse(refused if refused in WEB_REFUSALS
                                   else "inside_address", current)
                _line(current, "failed", code="no_answer", bytes_out=sent)
                return {"refused": "no_answer"}
            except (http.client.HTTPException, ValueError):
                _line(current, "failed", code="bad_answer", bytes_out=sent)
                return {"refused": "bad_answer"}
            finally:
                wire_headers = {}
            status = _web_status(resp)
            if keyed:
                cred = None  # a key rides the first hop only
                _fetch_key_answered(sid, current_host, status, caller="app_web")
            if status in _KEYED_REDIRECTS:
                location = _web_header(resp, "location")
                _web_close(resp)
                _line(current, "done", status=status, bytes_out=sent, bytes_in=0)
                if hop >= WEB_MAX_REDIRECTS:
                    return _refuse("redirects", current)
                nxt = urljoin(current, location) if location else ""
                code, nxt_host = _web_url_code(nxt)
                if not location or code == "not_allowed":
                    _line(current, "failed", code="bad_answer", bytes_out=0)
                    return {"refused": "bad_answer"}
                if code:
                    return _refuse(code, nxt)
                if _web_killed() or nxt_host not in _recorded_web_on(d):
                    return _refuse("redirect_off_list", nxt)
                if status == 303 or (status in (301, 302) and send_method == "POST"):
                    send_method = "HEAD" if send_method == "HEAD" else "GET"
                    send_body = None
                    send = {k: v for k, v in send.items() if k != "Content-Type"}
                current, current_host = nxt, nxt_host
                continue
            try:
                declared = _web_header(resp, "content-length").strip()
                if declared.isdigit() and int(declared) > cap_in:
                    _line(current, "refused", code="too_big", status=status,
                          bytes_out=sent, bytes_in=0)
                    return {"refused": "too_big"}
                data = b"" if send_method == "HEAD" else resp.read(cap_in + 1)
            except (OSError, http.client.HTTPException, ValueError):
                _line(current, "failed", code="no_answer", status=status,
                      bytes_out=sent)
                return {"refused": "no_answer"}
            finally:
                _web_close(resp)
            data = bytes(data) if isinstance(data, (bytes, bytearray)) else b""
            if len(data) > cap_in:
                _line(current, "refused", code="too_big", status=status,
                      bytes_out=sent, bytes_in=len(data))
                return {"refused": "too_big"}
            answer_headers = _web_answer_headers(resp, echoes)
            if echoes:
                data = _credential_scrub(echoes, data)
            if send_method != "HEAD" and "content-length" in answer_headers:
                # The length of what is handed back, not what the server said.
                answer_headers["content-length"] = str(len(data))
            _line(current, "done", status=status, bytes_out=sent,
                  bytes_in=len(data))
            return {"status": status, "headers": answer_headers, "body": data}
        return _refuse("redirects", current)  # pragma: no cover - loop returns
    except Exception as exc:  # noqa: BLE001 - never raise to the app's page
        logger.warning("browser app %s: outside request failed (%s)",
                       _shown(app), type(exc).__name__)
        _line(url, "failed", code="no_answer", bytes_out=0)
        return {"refused": "no_answer"}
    finally:
        cred = None
        echoes = ()


# ── build-time probe: one look at a catalog-listed keyless endpoint ──────────
#
# When the build engine drafts a feed app from a live-data service it found in a
# catalog, it needs to see ONE sample reply to know which fields hold a picture,
# a number or a list. That look happens while building, so it is governed by
# the build-network answer (like :mod:`puffpai.source_fetch`), not by the run
# posture, and it is not a door: nothing the finished bot does can reach it.
#
# It is deliberately narrower than any door:
#   * the caller must say the build is online (the wizard's own answer), and
#     ``PUFFPAI_BUILD_PROBES=off`` refuses every probe regardless;
#   * https on 443 only, exactly the declared host, a registrable public name,
#     every resolved address public, and the socket pinned to the address
#     that was checked (the name is never looked up twice);
#   * ZERO redirects -- a 3xx is recorded and refused, never followed;
#   * GET only, fixed headers, no caller kwargs at all (so no credential, no
#     cookie, no body), and the reply is read up to ``max_bytes`` and no more.

#: Operator switch that refuses every build-time probe. Only "off" is honoured.
BUILD_PROBE_ENV = "PUFFPAI_BUILD_PROBES"

#: Hard ceiling on what one probe reads, whatever the caller asks for.
BUILD_PROBE_MAX_BYTES = 512 * 1024

#: The one User-Agent every probe and feed sends. Several public APIs (for
#: example MusicBrainz and Wikidata) refuse anonymous clients; this names the
#: product and nothing about the household.
from puffpai import USER_AGENT as FEED_USER_AGENT  # noqa: E402

_PROBE_DOOR = Door(
    id="build_probe", hosts=(), purpose="One look at a sample reply while "
    "building.", data_sent="A request for the sample address only.",
    config_flag="build_probe", env_var=BUILD_PROBE_ENV,
    consent_kind="owner_flag", enforcement="registry", probe_ok=False,
    settings_hint="The build could not take a sample look at that service.",
    default=False, dynamic_hosts=False,
)


#: What the owner is told when the Bridge holds a sample look back.
SAMPLE_LOOK_HELD_LINE = (
    "The Bridge is up and \"Looking at a service before adding it\" is not "
    "switched on, so nothing was fetched. You can switch it on in Settings, "
    "Bridge switches, Service previews.")


def sample_look_held() -> bool:
    """True when the Bridge (or the owner's switch) keeps sample looks in. Fails closed."""
    try:
        from puffpai.security import outside_switches
        return outside_switches.is_off(SAMPLE_LOOK_SERVICE)
    except Exception:  # unreadable: held, never let out
        return True


def build_probe_get(url: str, *, host: str, build_online: bool,
                    max_bytes: int = BUILD_PROBE_MAX_BYTES,
                    timeout: float = DEFAULT_TIMEOUT,
                    resolver=None) -> dict[str, Any]:
    """Fetch one sample reply from *url* on *host*, or raise :class:`EgressBlocked`.

    Returns ``{"status", "content_type", "bytes", "truncated", "body"}`` where
    ``body`` is at most *max_bytes* (capped at :data:`BUILD_PROBE_MAX_BYTES`)
    raw bytes. The body is data; this function never parses or follows it.
    Every look -- and every refusal -- is one line in the outside-traffic log
    under the ``sample_look`` service, host only.
    """
    from puffpai.security import egress_ledger
    pd = _PROBE_DOOR

    def _refuse(reason: str, message: str, code: str) -> EgressBlocked:
        blocked = EgressBlocked(pd.id, reason, message, code=code)
        egress_ledger.record(SAMPLE_LOOK_SERVICE,
                             host=egress_ledger.safe_host(url),
                             outcome="refused", code=code, what="sample_look",
                             bytes_in=0, bytes_out=0)
        return blocked

    kill = os.environ.get(BUILD_PROBE_ENV)
    if kill is not None and _as_bool(kill) is False:
        raise _refuse(f"{BUILD_PROBE_ENV}=off",
                      "Sample looks at outside services are switched off on "
                      "this computer, so nothing was fetched.",
                      "turned_off_here")
    if _owner_switch(SAMPLE_LOOK_SERVICE) is False:
        raise _refuse("the owner switched sample looks off",
                      "Sample looks at outside services are switched off, so "
                      "nothing was fetched.", "switched_off")
    if sample_look_held():
        # With the Bridge up only an explicit ON lets a look
        # out (this file is a listed exit, so the connect-time backstop
        # cannot tell a look from any other request it makes).
        raise _refuse("the Bridge is up and sample looks are not switched on",
                      SAMPLE_LOOK_HELD_LINE, "bridge_up")
    if build_online is not True:
        raise _refuse("the build is not online",
                      "This build was set up to stay offline while building, "
                      "so nothing was fetched.", "bot_offline")
    want = _clean_host(host)
    if not is_registrable_hostname(want):
        raise _refuse(f"host {_shown(host)} is not a public name",
                      "That service's address was not a normal public web "
                      "address, so nothing was fetched.", "not_on_list")
    try:
        parts = urlsplit(str(url or "").strip())
        port = parts.port or 443
    except ValueError:
        raise _refuse("unparseable url", "That web address could not be read.",
                      "not_allowed")
    if parts.scheme.lower() != "https" or port != 443:
        raise _refuse("not https on 443",
                      "That request was refused because it was not an "
                      "ordinary encrypted connection.", "not_encrypted")
    if parts.username or parts.password:
        raise _refuse("url carries credentials", "That web address was refused.",
                      "not_allowed")
    got = _clean_host(parts.hostname or "")
    if got != want:
        raise _refuse(f"url host {got!r} is not {want!r}",
                      "That address was not on the service it claimed to be, "
                      "so nothing was fetched.", "not_on_list")
    try:
        answers = _check_resolved_public(pd, got, 443, resolver)
    except EgressBlocked as blocked:
        egress_ledger.record(SAMPLE_LOOK_SERVICE, host=got, outcome="refused",
                             code=reason_code(blocked), what="sample_look",
                             bytes_in=0, bytes_out=0)
        raise
    pin = _new_pin(resolver)
    pin_add(pin, got, answers)

    limit = max(1, min(int(max_bytes), BUILD_PROBE_MAX_BYTES))
    headers = {"User-Agent": FEED_USER_AGENT, "Accept": "application/json"}
    out = _bytes_out("GET", parts.geturl(), headers, None)
    try:
        resp = _send("GET", parts.geturl(), pin=pin, timeout=timeout,
                     allow_redirects=False, stream=True, headers=headers)
    except Exception:
        refused = getattr(pin, "refused", None)
        if refused is not None:
            raise _refuse(f"pinned connection refused: {refused.reason}",
                          "That request tried to reach an address on this "
                          "machine or this network, so nothing was sent.",
                          getattr(refused, "code", "inside_address")) from None
        egress_ledger.record(SAMPLE_LOOK_SERVICE, host=got, outcome="failed",
                             code="no_answer", what="sample_look",
                             bytes_out=out)
        raise
    total = 0
    try:
        _verify_peer(pd, resp, got)
        if resp.status_code in (301, 302, 303, 307, 308):
            egress_ledger.record(SAMPLE_LOOK_SERVICE, host=got, outcome="done",
                                 status=resp.status_code, what="sample_look",
                                 bytes_in=0, bytes_out=out)
            raise EgressBlocked(pd.id, f"redirect ({resp.status_code})",
                                "That service sent the request somewhere else, "
                                "so nothing was fetched.", code="redirected_away")
        chunks: list[bytes] = []
        truncated = False
        for chunk in resp.iter_content(chunk_size=16384):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                truncated = True
                break
        body = b"".join(chunks)[:limit]
        egress_ledger.record(SAMPLE_LOOK_SERVICE, host=got, outcome="done",
                             status=resp.status_code, what="sample_look",
                             bytes_in=total, bytes_out=out)
        return {"status": int(resp.status_code),
                "content_type": str(resp.headers.get("Content-Type") or "")[:120],
                "bytes": len(body), "truncated": truncated, "body": body}
    finally:
        try:
            resp.close()
        except Exception:  # pragma: no cover - a stub transport
            pass


#: The outside-services id sample looks are logged and switched under.
SAMPLE_LOOK_SERVICE = "sample_look"


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
    "APPS_KILL_ENV",
    "OPEN_POSTURE",
    "BUILD_PROBE_ENV",
    "SAMPLE_LOOK_SERVICE",
    "BUILD_PROBE_MAX_BYTES",
    "FEED_USER_AGENT",
    "WEB_ANSWER_HEADERS",
    "WEB_DOOR_SUFFIX",
    "WEB_MAX_REDIRECTS",
    "WEB_METHODS",
    "WEB_REFUSALS",
    "WEB_REQUEST_HEADERS",
    "app_door",
    "app_door_consent",
    "app_web_hosts",
    "app_web_request",
    "clear_web_app_doors",
    "register_web_app_door",
    "set_app_web_hosts",
    "switch_off_web_hosts",
    "unregister_web_app_door",
    "web_app_door",
    "web_app_ids",
    "web_door_id",
    "build_probe_get",
    "door_states",
    "internal_address_reason",
    "is_app_door",
    "is_ip_literal",
    "is_registrable_hostname",
    "set_app_door_consent",
    "reason_code",
    "resolve_addresses",
    "run_posture",
    "settings_hint",
    "shut_reason",
    "KEYED_DOORS",
    "MAX_FEED_DOOR_HOSTS",
    "RESULT_PICTURES",
    "RESULT_PICTURE_MAX_BYTES",
    "feed_doors",
    "register_feed_doors",
    "result_picture_get",
    "sample_look_held",
    "SAMPLE_LOOK_HELD_LINE",
    "setup_check_hop",
    "fetch_key_hosts",
    "CREDENTIAL_PLACEMENTS",
]
