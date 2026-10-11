"""Severe-weather alerts, answered from the alert source and nothing else.

Why this module exists
----------------------
"any severe weather alerts" must not come back as, in one breath:

    "No severe weather alerts are currently active in your area. The
     system couldn't retrieve any alerts because it doesn't have your
     location coordinates. ... Add or confirm your coordinates in Settings"

Two claims, mutually exclusive, and the second one false when "what's the
weather like right now" answers with real local conditions. A false all-clear
on a severe-weather path is a safety-relevant fabrication, not a cosmetic bug.

Two traps, both real:

1. **A shut door, while the coordinates are already on the box.**
   ``prep_weather_alerts`` resolves a place name to coordinates through the
   ``geocode`` door (nominatim). A box can have ``geocode`` ``false`` while
   ``weather`` (open-meteo AND ``api.weather.gov``) is ``true``, which is
   why the weather card works. The tool then returns its honest "[Egress door
   closed] ... I have no coordinates" refusal for a check that the open
   ``weather`` door could answer.  This module resolves the household's coordinates the way the
   working weather card does and asks the NWS through the door that is open.
   It never opens a door, never widens a host list, and never guesses a
   location.

2. **A refusal handed to an LLM to "summarize".** Run through
   ``_summarize_tool_result``, a failure message can come back with a
   confident all-clear prepended by the model. Nothing here
   goes near a model: :func:`answer` returns text built from the fetched data
   by :func:`format_alert_reply`, so the four states — alerts present, none
   active, source unreachable, location unknown — are each stated exactly
   once and never blended.
"""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

#: What the reply names as the authority it actually consulted.
NWS_SOURCE = "the National Weather Service"
AUTOMATION_SOURCE = "the household weather-alert automation"

#: Alert states. Exactly one is reported per turn — see the module docstring.
STATE_ACTIVE = "active"
STATE_NONE = "none"
STATE_SOURCE_DOWN = "source_down"
STATE_LOCATION_UNSET = "location_unset"
STATE_DOOR_CLOSED = "door_closed"


def household_coordinates():
    """The household's own coordinates: ``(lat, lon)`` or ``(None, None)``.

    Delegates to :mod:`puffpai.household_geo`, the ONE resolver the weather
    card also uses: ``config.latitude``/``longitude`` when set, otherwise one
    cached geocode of ``config.location`` through the ``geocode`` switch if
    the owner opened it, otherwise unknown. There is no built-in fallback, so
    ``(None, None)`` means "we genuinely do not know where this house is",
    and that is the only thing that may be reported as a location problem.
    """
    from puffpai.household_geo import household_coordinates as _resolve
    return _resolve()


def household_location() -> str:
    """The configured place name, for the reply text only."""
    from puffpai.household_geo import household_location as _resolve
    return _resolve()


def _alerts_from_nws(lat, lon):
    """Active NWS alerts for a point, through the ``weather`` egress door.

    ``api.weather.gov`` is one of that door's declared hosts, so this adds no
    outbound host and crosses no closed door. Returns ``(state, alerts,
    detail)``.
    """
    from puffpai.security.egress_doors import EgressBlocked, egress_get
    try:
        resp = egress_get(
            "weather",
            "https://api.weather.gov/alerts/active",
            params={"point": "%s,%s" % (lat, lon)},
            headers={"User-Agent": "PuffPAI/1.0"},
            timeout=10,
        )
    except EgressBlocked as blocked:
        return STATE_DOOR_CLOSED, [], blocked.user_message
    except Exception as exc:
        # Type only: a requests error message quotes the URL, and the URL's
        # query string is the house's position.
        logger.debug("weather-alerts: NWS fetch failed: %s", type(exc).__name__)
        return STATE_SOURCE_DOWN, [], type(exc).__name__
    if getattr(resp, "status_code", None) != 200:
        return (STATE_SOURCE_DOWN, [],
                "NWS returned HTTP %s" % getattr(resp, "status_code", "?"))
    try:
        features = (resp.json() or {}).get("features") or []
    except Exception as exc:
        logger.debug("weather-alerts: NWS payload unreadable: %s", exc)
        return STATE_SOURCE_DOWN, [], "NWS returned unreadable data"
    alerts = []
    for f in features:
        props = (f or {}).get("properties") or {}
        alerts.append({
            "event": props.get("event") or "Weather alert",
            "severity": props.get("severity") or "Unknown",
            "headline": props.get("headline") or "",
        })
    return (STATE_ACTIVE if alerts else STATE_NONE), alerts, ""


def _alerts_from_automation(lat, lon, location):
    """The Node-RED ``weather_alerts`` flow — the local-first authority.

    Returns ``(state, alerts, detail)``; ``STATE_SOURCE_DOWN`` whenever the
    automation engine does not answer with a usable payload, so the caller
    can fall through to the NWS without ever reporting a guess.
    """
    try:
        from puffpai.automation_client import AutomationClient
        # Short and single-shot on purpose: this is an interactive turn, the
        # flow is on the box's own LAN, and the NWS fallback below is the same
        # authority the flow itself reads. The client's defaults (2 retries x
        # 10s) would spend 20 seconds before that fallback got a turn.
        ok, data = AutomationClient(timeout=4, max_retries=1).call(
            "weather_alerts",
            {"location": location, "lat": lat, "lon": lon})
    except Exception as exc:
        logger.debug("weather-alerts: automation call failed: %s",
                     type(exc).__name__)
        return STATE_SOURCE_DOWN, [], type(exc).__name__
    if not ok or not isinstance(data, dict):
        # A fixed phrase, not the payload: whatever came back could echo the
        # point we sent.
        return (STATE_SOURCE_DOWN, [],
                "automation did not answer" if not ok
                else "automation payload was not an object")
    raw = data.get("alerts")
    alerts = []
    for a in (raw or []):
        if not isinstance(a, dict):
            continue
        alerts.append({
            "event": a.get("event") or "Weather alert",
            "severity": a.get("severity") or "Unknown",
            "headline": a.get("headline") or "",
        })
    if alerts:
        return STATE_ACTIVE, alerts, ""
    try:
        count = int(data.get("alert_count", 0))
    except (TypeError, ValueError):
        count = 0
    if count > 0:
        # A count with no list is not something to render as "none active".
        return STATE_SOURCE_DOWN, [], "automation reported %d alerts with no detail" % count
    if "alert_count" not in data:
        return STATE_SOURCE_DOWN, [], "automation payload carried no alert_count"
    return STATE_NONE, [], ""


def alert_status(location=None, lat=None, lon=None) -> dict:
    """Ask the real alert source and return WHAT IT SAID, as data.

    Never blends states: the returned ``state`` is one of the five module
    constants, and the caller renders exactly that one.
    """
    location = (location or household_location() or "").strip()
    if lat is None or lon is None:
        lat, lon = household_coordinates()
    if lat is None or lon is None:
        return {"state": STATE_LOCATION_UNSET, "location": location,
                "alerts": [], "source": "", "detail": ""}

    state, alerts, detail = _alerts_from_automation(lat, lon, location)
    source = AUTOMATION_SOURCE
    if state == STATE_SOURCE_DOWN:
        state, alerts, detail = _alerts_from_nws(lat, lon)
        source = NWS_SOURCE
    if not location:
        location = "your area"
    return {"state": state, "location": location, "alerts": alerts,
            "source": source, "detail": detail}


def format_alert_reply(status: dict, now=None) -> str:
    """Render one state, in plain words, with no second claim attached."""
    now = now or datetime.now()
    stamp = now.strftime("%I:%M %p").lstrip("0")
    location = status.get("location") or "your area"
    source = status.get("source") or NWS_SOURCE
    state = status.get("state")

    if state == STATE_ACTIVE:
        alerts = status.get("alerts") or []
        head = ("%d active severe weather alert%s for %s as of %s (from %s):"
                % (len(alerts), "" if len(alerts) == 1 else "s",
                   location, stamp, source))
        lines = []
        for a in alerts:
            line = "- %s (%s)" % (a.get("event"), a.get("severity"))
            headline = (a.get("headline") or "").strip()
            if headline:
                line += "\n  %s" % headline
            lines.append(line)
        return head + "\n" + "\n".join(lines)

    if state == STATE_NONE:
        return ("No active severe weather alerts for %s as of %s (checked %s)."
                % (location, stamp, source))

    if state == STATE_LOCATION_UNSET:
        from puffpai.household_geo import UNSET_MESSAGE
        return ("I can't check for weather alerts: %s Once it's set I'll "
                "check the National Weather Service from there."
                % UNSET_MESSAGE)

    if state == STATE_DOOR_CLOSED:
        return ("[Egress door closed] %s\n\nI did not check for weather "
                "alerts, so I can't tell you whether any are active."
                % (status.get("detail") or "").strip())

    # STATE_SOURCE_DOWN — say only this. Never "no alerts".
    return ("I couldn't check for weather alerts just now — %s didn't answer. "
            "I don't know whether any are active, so please check NOAA Weather "
            "Radio (162.400-162.550 MHz) or another source if weather is "
            "threatening." % source)


def answer(location=None, lat=None, lon=None, now=None) -> str:
    """The whole deterministic reply for "any severe weather alerts"."""
    return format_alert_reply(alert_status(location=location, lat=lat, lon=lon),
                              now=now)
