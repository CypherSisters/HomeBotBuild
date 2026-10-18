"""
PuffPAI Dashboard Widget System — the modular, LLM-managed dashboard.

Generalizes the camera-tile pattern: ONE registry, ONE catalog, generic
agent tools. Adding a new widget type = a catalog entry + a frontend
renderer — no new endpoints, tools, or routing per use case.

Catalog entries declare a consent_class so gating can be enforced per type:
  - internal:        no gate (calendar, weather, ...)
  - device:          Sentinel per-device consent (cameras — handled by
                     dashboard_registry, rendered by the Security Cameras
                     panel, exposed here as virtual read-only widgets)
  - external_domain: outbound traffic — Shield domain whitelist.
                     NOTE: gating for external_domain is currently OFF
                     (dev mode); every fetch is still logged to
                     security_logs/widget_egress.jsonl so the Shield can be
                     flipped on later without data loss.

Registry persists at data/dashboard_widgets.json ($PUFFPAI_DATA_DIR when set).

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from puffpai.statedir import resolve_runtime_dir

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Both paths are resolved per call, never frozen at import: under the hardened
# systemd unit only $PUFFPAI_DATA_DIR is writable, and an import-time constant
# would also drag puffpai.config (and the secrets provider) into the import
# graph. Each name below survives as a test/ops override hook — set it to a
# path and the accessor returns that verbatim; leave it None and the env
# decides, defaulting to the historical in-tree location.
_REGISTRY_PATH: str | None = None
_EGRESS_LOG: str | None = None


def _registry_path() -> str:
    """Widget registry — historically a ``data/`` file, so a relocated data dir
    holds it at its root (same convention as agent_events.jsonl)."""
    if _REGISTRY_PATH:
        return _REGISTRY_PATH
    return os.path.join(
        resolve_runtime_dir(os.path.join(_REPO_ROOT, "data")),
        "dashboard_widgets.json")


def _egress_log_path() -> str:
    """Egress ledger — a security log, so it keeps its own subtree under a
    relocated data dir (same convention as security/audit_log.py)."""
    if _EGRESS_LOG:
        return _EGRESS_LOG
    return os.path.join(
        resolve_runtime_dir(os.path.join(_REPO_ROOT, "security_logs"),
                            env_subdir="security_logs"),
        "widget_egress.jsonl")


_lock = threading.Lock()

# ── Widget catalog ───────────────────────────────────────────────────────────

WIDGET_CATALOG: dict[str, dict] = {
    "news": {
        "title": "News Feed",
        "consent_class": "external_domain",
        "props": {"url": "(required unless source given)", "source": "shortcut name",
                  "title": "display title", "max_items": "default 6"},
        "description": "Headlines from an RSS/Atom feed, fetched server-side.",
    },
    "calendar": {
        "title": "Calendar",
        "consent_class": "internal",
        "props": {"title": "display title", "days_ahead": "default 7"},
        "description": "Upcoming events and tasks from the PuffPAI calendar.",
    },
    "alarmflow": {
        "title": "Timers & Alarms",
        "consent_class": "internal",
        "props": {"title": "display title"},
        "description": "Timers & Alarms: active countdowns at a glance. "
                       "Reads live from app_data/alarmflow.json.",
    },
    "camera": {
        "title": "Security Camera",
        "consent_class": "device",
        "props": {"entity_id": "HA camera entity"},
        "description": "Live camera tile. Add via dashboard_add_camera "
                       "(Sentinel consent applies); shown in the Security "
                       "Cameras panel.",
    },
    "now-playing": {
        "title": "Now Playing",
        "consent_class": "internal",
        "props": {"title": "display title"},
        "description": "Jellyfin now-playing: album art, transport controls, "
                       "volume. LAN-only (media Pi); works in Lockdown.",
    },
    # One installed app's live card (picture, number or list). The widget holds
    # no address of its own: it shows what the app's feed already fetched on
    # the server, through that app's own outside service, which stays shut
    # until the owner allows it. The browser only ever talks to this box.
    "app-feed": {
        "title": "Live card",
        "consent_class": "external_domain",
        "props": {"app_id": "an installed app that shows live data",
                  "title": "display title"},
        "description": "The live card of one installed app, from data this "
                       "box fetched and stored. Needs that app's outside "
                       "service allowed by the owner.",
    },
}

# Known feeds so the LLM never has to invent URLs.
FEED_DIRECTORY: dict[str, dict] = {
    "npr":          {"url": "https://feeds.npr.org/1001/rss.xml", "title": "NPR News"},
    "bbc":          {"url": "https://feeds.bbci.co.uk/news/rss.xml", "title": "BBC News"},
    "hacker news":  {"url": "https://news.ycombinator.com/rss", "title": "Hacker News"},
    "ars technica": {"url": "https://feeds.arstechnica.com/arstechnica/index", "title": "Ars Technica"},
    "the guardian": {"url": "https://www.theguardian.com/world/rss", "title": "The Guardian"},
    "the verge":    {"url": "https://www.theverge.com/rss/index.xml", "title": "The Verge"},
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Registry persistence ─────────────────────────────────────────────────────

def _load() -> list[dict]:
    try:
        with open(_registry_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save(items: list[dict]) -> None:
    path = _registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


#: Widget types that read one of the first apps' data (puffpai.first_apps).
#: While that app is turned off the widget is neither added nor shown.
WIDGET_APP = {"calendar": "calendar", "alarmflow": "alarmflow"}


def _app_off(wtype: str) -> str:
    """The app's turned-off line when this widget type's app is off, else ""."""
    app_id = WIDGET_APP.get(wtype)
    if not app_id:
        return ""
    try:
        from puffpai import first_apps
        return first_apps.turned_off_line(app_id) if first_apps.is_off(app_id) else ""
    except Exception:  # noqa: BLE001 - never hide a widget over this check
        return ""


def list_widgets(include_cameras: bool = False) -> list[dict]:
    items = [w for w in _load() if not _app_off(str(w.get("type") or ""))]
    if include_cameras:
        try:
            from puffpai import dashboard_registry as _dreg
            for cam in _dreg.list_cameras():
                items.append({
                    "id": f"cam::{cam['entity_id']}",
                    "type": "camera",
                    "props": {"entity_id": cam["entity_id"], "title": cam.get("label")},
                    "added_by": cam.get("added_by"),
                    "added_at": cam.get("added_at"),
                    "virtual": True,  # managed by dashboard_registry
                })
        except Exception:
            pass
    return items


def add_widget(wtype: str, props: dict, added_by: str = "agent") -> tuple[bool, str, dict | None]:
    """Add a widget. Returns (ok, message, widget)."""
    wtype = (wtype or "").strip().lower()
    if wtype not in WIDGET_CATALOG:
        known = ", ".join(sorted(WIDGET_CATALOG))
        return False, f"Unknown widget type '{wtype}'. Known types: {known}.", None
    if wtype == "camera":
        return False, ("Cameras are Sentinel-gated. Use dashboard_add_camera "
                       "(or POST /dashboard/cameras) instead."), None
    off = _app_off(wtype)
    if off:
        return False, off, None

    props = dict(props or {})
    if wtype == "news":
        source = (props.pop("source", "") or "").strip().lower()
        if source and not props.get("url"):
            hit = FEED_DIRECTORY.get(source)
            if not hit:
                # loose match
                hit = next((v for k, v in FEED_DIRECTORY.items()
                            if source in k or k in source), None)
            if not hit:
                known = ", ".join(sorted(FEED_DIRECTORY))
                return False, (f"I don't know a feed for '{source}'. Known "
                               f"sources: {known}. Or give me a feed URL."), None
            props.setdefault("title", hit["title"])
            props["url"] = hit["url"]
        if not props.get("url"):
            known = ", ".join(sorted(FEED_DIRECTORY))
            return False, (f"A news widget needs a source or URL. Known "
                           f"sources: {known}."), None
        props.setdefault("title", "News")
        props["max_items"] = int(props.get("max_items", 6) or 6)
    elif wtype == "calendar":
        props.setdefault("title", "Upcoming")
        props["days_ahead"] = int(props.get("days_ahead", props.pop("days", 7)) or 7)
    elif wtype == "now-playing":
        props.setdefault("title", "Now Playing")
    elif wtype == "alarmflow":
        props.setdefault("title", "Timers & Alarms")
    elif wtype == "app-feed":
        problem = _app_feed_props(props)
        if problem:
            return False, problem, None

    widget = {
        "id": "w_" + uuid.uuid4().hex[:8],
        "type": wtype,
        "props": props,
        "added_by": added_by,
        "added_at": _utcnow(),
    }
    with _lock:
        items = _load()
        # Dedupe: same type + same url/title
        for w in items:
            if w["type"] == wtype and (
                    (wtype == "news" and w["props"].get("url") == props.get("url"))
                    or (wtype == "app-feed"
                        and w["props"].get("app_id") == props.get("app_id"))
                    or (wtype in ("calendar", "now-playing", "alarmflow"))):
                return False, (f"A {wtype} widget"
                               f"{' for that feed' if wtype == 'news' else ''} "
                               f"is already on the dashboard "
                               f"('{w['props'].get('title')}')."), w
        items.append(widget)
        _save(items)
    return True, (f"Done, '{props.get('title', wtype)}' ({wtype} widget) is "
                  f"now on the dashboard."), widget


def _app_feed_props(props: dict) -> str:
    """Check an app-feed widget's props in place. '' when usable.

    Only an installed app with a valid live-data block may be named; the
    props are cut down to the app id and a bounded title, so nothing else an
    agent or a caller supplies (an address, a host) is ever stored.
    """
    from puffpai import app_registry, feeds
    app_id = str(props.get("app_id") or "").strip()
    if not app_registry.valid_id(app_id):
        return "Which app? Give the name of an installed app that shows live data."
    match = next((m for m, _f in feeds.feed_apps() if m.get("id") == app_id), None)
    if match is None:
        return (f"No installed app called '{app_id}' shows live data, so there "
                f"is nothing to put on the dashboard.")
    title = str(props.get("title") or match.get("name") or app_id)[:64]
    props.clear()
    props.update({"app_id": app_id, "title": title})
    return ""


def remove_widget(query: str) -> tuple[bool, str]:
    q = (query or "").strip().lower().strip("'\"")
    if not q:
        return False, "Which widget? Give me its title or type."
    with _lock:
        items = _load()
        for w in items:
            hay = f"{w['id']} {w['type']} {w['props'].get('title','')}".lower()
            if q == w["id"].lower() or q == w["type"] or all(
                    t in hay for t in q.split()):
                items.remove(w)
                _save(items)
                return True, (f"Removed '{w['props'].get('title', w['type'])}' "
                              f"from the dashboard.")
    return False, f"No dashboard widget matches '{query}'."


def configure_widget(widget_id: str, props: dict) -> tuple[bool, str]:
    with _lock:
        items = _load()
        for w in items:
            if w["id"] == widget_id:
                w["props"].update(props or {})
                _save(items)
                return True, f"Updated '{w['props'].get('title', w['type'])}'."
    return False, f"No widget with id {widget_id}."


def get_widget(widget_id: str) -> dict | None:
    return next((w for w in _load() if w["id"] == widget_id), None)


# ── Environment (forecast hi/lo + air quality; open-meteo, keyless) ─────────

_env_cache: dict[str, tuple[float, dict]] = {}
_ENV_TTL = 1800  # 30 min

# No built-in coordinates. The house's position comes from
# ``puffpai.household_geo`` (config latitude/longitude, else one cached
# geocode through the ``geocode`` switch, else unknown), the same resolver the
# severe-weather alerts use, so the card and the alerts always describe the
# same place.

_AQI_LABELS = [(50, "Good"), (100, "Moderate"), (150, "Unhealthy (sensitive)"),
               (200, "Unhealthy"), (300, "Very unhealthy"), (10 ** 6, "Hazardous")]


_RISK_LABELS = ["Low", "Moderate", "High", "Very High"]


def _risk(level: int, detail: str) -> dict:
    level = min(3, max(0, int(level)))
    return {"level": level, "label": _RISK_LABELS[level], "detail": detail}


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _air_breakdown(p: dict, rh, precip) -> dict:
    """Health-focused air view for the weather card: mold, dust,
    pollen/allergens, and an asthma composite. Levels 0-3
    (Low/Moderate/High/Very High), each with a short 'why' string.

    Bands follow EPA AQI breakpoints where a pollutant is measured.
    Pollen: open-meteo's CAMS pollen coverage is Europe-only, so outside it
    there is no pollen reading and the breakdown has no ``pollen`` entry
    (the card shows no pollen chip). A calendar guess would be wrong for
    most places and would also raise the asthma composite on no evidence.
    Informational only, not medical advice."""
    b: dict = {}

    # Mold — moisture-driven: relative humidity + precipitation
    lvl, drivers = 0, []
    rh = _num(rh)
    if rh is not None:
        drivers.append(f"humidity {round(rh)}%")
        if rh >= 85:
            lvl = 2
        elif rh >= 65:
            lvl = 1
    precip = _num(precip)
    if precip is not None and precip > 0:
        drivers.append("rain today")
        lvl = min(3, lvl + 1)
    b["mold"] = _risk(lvl, ", ".join(drivers) or "no humidity data")

    # Dust — PM10 (EPA bands) plus mineral/Saharan dust when reported
    pm10, dust = _num(p.get("pm10")), _num(p.get("dust"))
    lvl, detail = 0, "no data"
    if pm10 is not None:
        lvl = 0 if pm10 <= 54 else 1 if pm10 <= 154 else 2 if pm10 <= 254 else 3
        detail = f"PM10 {round(pm10)} ug/m3"
    if dust is not None and dust >= 20:
        lvl = min(3, lvl + 1)
        detail += f", airborne dust {round(dust)} ug/m3"
    b["dust"] = _risk(lvl, detail)

    # Pollen / allergens: only when the API measured it
    pollen = {k[:-7]: _num(v) for k, v in p.items()
              if k.endswith("_pollen") and _num(v) is not None}
    if pollen:
        worst = max(pollen, key=lambda k: pollen[k])
        wv = pollen[worst]
        lvl = 0 if wv < 20 else 1 if wv < 80 else 2 if wv < 200 else 3
        b["pollen"] = _risk(lvl, f"{worst} {round(wv)} grains/m3")

    # Asthma — composite: worst of AQI band / PM2.5 / ozone, nudged up when
    # dust or pollen is elevated (common asthma triggers)
    aqi, pm25, o3 = _num(p.get("us_aqi")), _num(p.get("pm2_5")), _num(p.get("ozone"))
    lvl, drivers = 0, []
    if aqi is not None:
        lvl = 0 if aqi <= 50 else 1 if aqi <= 100 else 2 if aqi <= 150 else 3
        drivers.append(f"AQI {round(aqi)}")
    if pm25 is not None and pm25 >= 35:
        lvl = max(lvl, 2)
        drivers.append(f"PM2.5 {round(pm25)}")
    if o3 is not None and o3 >= 120:
        lvl = max(lvl, 2)
        drivers.append(f"ozone {round(o3)} ug/m3")
    pollen_lvl = b["pollen"]["level"] if "pollen" in b else 0
    lvl = max(lvl, b["dust"]["level"] - 1, pollen_lvl - 1)
    b["asthma"] = _risk(lvl, ", ".join(drivers) or "air data unavailable")
    return b


def fetch_environment(lat: float | None = None, lon: float | None = None) -> dict:
    """Daily high/low, US AQI, pollutant detail, and a health-focused air
    breakdown (mold / dust / pollen / asthma) from open-meteo (no API key).
    Cached 30 min; egress logged for the Shield like all widget traffic.

    ``lat``/``lon`` default to the household's own position from
    :mod:`puffpai.household_geo`. When that is unknown the result is
    ``{"unset": <message>}``: nothing is fetched, nothing is cached, and the
    card says the coordinates are not set instead of showing another city.

    Offline-Core: EDGE feature. Declared egress: api.open-meteo.com and
    air-quality-api.open-meteo.com only. Fails gracefully — the weather card
    just shows less; no CORE path depends on this."""
    if lat is None or lon is None:
        from puffpai.household_geo import UNSET_MESSAGE, household_coordinates
        lat, lon = household_coordinates()
        if lat is None or lon is None:
            return {"unset": UNSET_MESSAGE}
    # The switches are part of the key: switching one on (or off) takes
    # effect on the next poll, and a forecast is not asked for again on every
    # load while air quality stays off.
    key = f"{lat:.3f},{lon:.3f}|{_door_open('weather')}|{_door_open('air_quality')}"
    now = time.time()
    cached = _env_cache.get(key)
    if cached and now - cached[0] < _ENV_TTL:
        return cached[1]
    from puffpai.security.egress_doors import EgressBlocked, egress_get
    out: dict = {}
    rh = precip = None
    try:
        f = egress_get(
            "weather",
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon,
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
                    # The current reading too, in the same one request, so
                    # a build with no home hub still has a temperature now.
                    "current": ("relative_humidity_2m,temperature_2m,"
                                "apparent_temperature,weather_code"),
                    "temperature_unit": "fahrenheit", "timezone": "auto",
                    "forecast_days": 1},
            timeout=8).json()
        out["high"] = round(f["daily"]["temperature_2m_max"][0])
        out["low"] = round(f["daily"]["temperature_2m_min"][0])
        try:
            precip = (f["daily"].get("precipitation_sum") or [None])[0]
            rh = f.get("current", {}).get("relative_humidity_2m")
            cur = f.get("current") or {}
            out["now"] = {"temp": _num(cur.get("temperature_2m")),
                          "feels_like": _num(cur.get("apparent_temperature")),
                          "humidity": _num(rh),
                          "code": _num(cur.get("weather_code"))}
        except Exception:
            pass
        _log_egress("https://api.open-meteo.com/v1/forecast", True, "daily hi/lo",
                    kind=KIND_WEATHER)
    except EgressBlocked as blocked:
        # The card must say the door is shut, not quietly show yesterday's
        # numbers or invent a forecast.
        out["blocked"] = blocked.user_message
        _log_egress("https://api.open-meteo.com/v1/forecast", False,
                    "egress door closed", kind=KIND_WEATHER)
    except Exception as exc:
        _log_egress("https://api.open-meteo.com/v1/forecast", False,
                    _exc_detail(exc), kind=KIND_WEATHER)
    pollutants: dict = {}
    try:
        a = egress_get(
            "air_quality",
            "https://air-quality-api.open-meteo.com/v1/air-quality",
            params={"latitude": lat, "longitude": lon,
                    "current": ("us_aqi,pm2_5,pm10,ozone,nitrogen_dioxide,dust,"
                                "alder_pollen,birch_pollen,grass_pollen,"
                                "mugwort_pollen,olive_pollen,ragweed_pollen")},
            timeout=8).json()
        pollutants = a.get("current", {}) or {}
        aqi = pollutants.get("us_aqi")
        out["us_aqi"] = aqi
        out["pm2_5"] = pollutants.get("pm2_5")
        if isinstance(aqi, (int, float)):
            out["aqi_label"] = next(lbl for cap, lbl in _AQI_LABELS if aqi <= cap)
        _log_egress("https://air-quality-api.open-meteo.com/v1/air-quality", True,
                    f"us_aqi={aqi}", kind=KIND_AIR_QUALITY)
    except EgressBlocked as blocked:
        out.setdefault("blocked", blocked.user_message)
        _log_egress("https://air-quality-api.open-meteo.com/v1/air-quality",
                    False, "egress door closed", kind=KIND_AIR_QUALITY)
    except Exception as exc:
        _log_egress("https://air-quality-api.open-meteo.com/v1/air-quality", False,
                    _exc_detail(exc), kind=KIND_AIR_QUALITY)
    out["air_breakdown"] = _air_breakdown(pollutants, rh, precip)
    # Kept when the forecast came back; the switches in the key make a
    # change in Settings take effect on the next poll, not 30 minutes later.
    if "high" in out:
        _env_cache[key] = (now, out)
    return out


def _door_open(door_id: str) -> bool:
    """Is this outside service's switch open right now? Fails closed."""
    try:
        from puffpai.security import egress_doors
        return bool(egress_doors.egress_allowed(door_id)[0])
    except Exception:  # noqa: BLE001
        return False


# ── News fetching (server-side; all widget egress goes through here) ─────────

_news_cache: dict[str, tuple[float, list[dict]]] = {}
_NEWS_TTL = 1800  # 30 min; the widget refreshes every 5 min, so 300 s always missed


#: A whole URL sitting inside some other text.
_DETAIL_URL_RE = re.compile(r"(?i)\bhttps?://\S+")
#: A query string, with or without a scheme in front of it. ``requests``
#: exception messages quote the request as a BARE PATH —
#: ``Max retries exceeded with url: /v1/forecast?latitude=..&longitude=..`` —
#: so matching only full URLs would miss the leak entirely.
_DETAIL_QUERY_RE = re.compile(r"\?\S*")


def _exc_detail(exc: BaseException) -> str:
    """The only thing an exception may contribute to the ledger: its TYPE.

    ``str(exc)`` on a ``requests`` failure quotes the request URL, and these
    widget URLs carry the household's latitude and longitude in the query
    string, so a flaky connection would write the family's coordinates into
    ``widget_egress.jsonl`` on every retry. The type name is what a reader of
    that log actually needs ("ConnectionError" vs "Timeout" vs
    "JSONDecodeError"); the message is not.
    """
    return type(exc).__name__


def _safe_detail(detail: object) -> str:
    """A ledger ``detail`` with anything URL- or query-shaped stripped out.

    Belt and braces behind :func:`_exc_detail`: callers are supposed to pass a
    type name or a short literal, but this module has ~a dozen ``_log_egress``
    call sites and the next one added must not be able to reintroduce the leak
    by reaching for ``str(exc)`` again. Hosts and paths are kept (they are
    already in the record's ``url`` field and carry no data); queries and
    fragments are not.
    """
    text = str(detail if detail is not None else "")[:200]
    if not text:
        return ""
    from puffpai.security import egress_ledger

    def _reduce(match):
        host, path = egress_ledger.safe_parts(match.group(0))
        return f"{host}{path}" if host else "<url>"

    text = _DETAIL_URL_RE.sub(_reduce, text)
    # Whatever is left that still starts a query string is dropped wholesale,
    # up to the next whitespace. Erring toward removing too much: a detail
    # field is a diagnostic hint, not evidence.
    text = _DETAIL_QUERY_RE.sub("?<redacted>", text)
    return text[:200]


#: What the record's ``kind`` says for each widget's fetch, so the open-meteo
#: forecast and air-quality lines are not filed as news fetches: a security
#: log that misdescribes what left the house is worse than no label at all.
KIND_NEWS = "widget_news_fetch"
KIND_WEATHER = "widget_weather_fetch"
KIND_AIR_QUALITY = "widget_air_quality_fetch"


def _log_egress(url: str, ok: bool, detail: str = "",
                kind: str = KIND_NEWS) -> None:
    """Shield hook: every outbound widget fetch is logged.

    ``enforced`` is True: every fetch in this module goes through
    ``security.egress_doors``, which refuses a shut door or an undeclared host
    before a socket opens. Writing ``false`` would make this security log
    understate what is actually in force.

    ``detail`` is sanitised on the way in too: a raw ``str(exc)[:200]`` from a
    ``requests`` error quotes the request URL, which for these widgets carries
    the household's latitude and longitude.

    Written through ``security.egress_ledger``, which rotates: unrotated, this
    file grows without bound, which is the failure mode ``puffpai-logcap``
    exists to mop up in ``/var/log`` and nothing watches for on the data
    partition. The URL is sanitised on the
    way in — query strings carry data and do not belong in a log.
    """
    from puffpai.security import egress_ledger
    egress_ledger.append({
        "ts": _utcnow(), "kind": kind or KIND_NEWS,
        "url": egress_ledger.safe_url(url), "ok": ok,
        "detail": _safe_detail(detail), "enforced": True,
    }, path=_egress_log_path())


def _parse_feed(content: bytes) -> list[dict]:
    """Minimal RSS 2.0 / Atom parser — no external deps."""
    root = ET.fromstring(content)

    def tag(el):  # strip namespace
        return el.tag.split("}")[-1].lower()

    items: list[dict] = []
    for el in root.iter():
        if tag(el) not in ("item", "entry"):
            continue
        title, link, published = "", "", ""
        for child in el:
            t = tag(child)
            if t == "title":
                title = (child.text or "").strip()
            elif t == "link":
                link = (child.get("href") or child.text or "").strip()
            elif t in ("pubdate", "published", "updated") and not published:
                published = (child.text or "").strip()
        if title:
            items.append({"title": title, "link": link, "published": published})
    return items


def fetch_news(url: str, max_items: int = 6) -> tuple[bool, list[dict] | str]:
    """Fetch + parse a feed with caching. Returns (ok, items|error)."""
    from puffpai.security.egress_doors import (
        EgressBlocked, egress_allowed, egress_get)
    now = time.time()
    cached = _news_cache.get(url)
    if cached and now - cached[0] < _NEWS_TTL:
        # With a 30 min TTL, an owner who switches the RSS door off must not
        # keep seeing headlines for half an hour. egress_allowed is a local
        # read of the switch (no network, no ledger line); closed, or
        # unreadable, skips the cache so the door's own refusal shows below.
        try:
            door_open = egress_allowed("news_rss")[0]
        except Exception:
            door_open = False
        if door_open:
            return True, cached[1][:max_items]
    try:
        # The door refuses any host outside FEED_DIRECTORY, so a feed URL that
        # reached this function from a config edit or an LLM cannot make the
        # appliance fetch an arbitrary site.
        resp = egress_get("news_rss", url, timeout=8, headers={
            "User-Agent": "PuffPAI/1.0 (local dashboard widget)"})
        resp.raise_for_status()
        items = _parse_feed(resp.content)
    except EgressBlocked as blocked:
        _log_egress(url, False, "egress door closed")
        return False, blocked.user_message
    except Exception as exc:
        _log_egress(url, False, _exc_detail(exc))
        if cached:  # stale-but-usable
            return True, cached[1][:max_items]
        return False, f"Could not fetch the feed: {exc}"
    _log_egress(url, True, f"{len(items)} items")
    _news_cache[url] = (now, items)
    return True, items[:max_items]


# ── The home page and sidebar layout (one plain JSON file) ───────────────────
#
# ``<state dir>/dashboard_layout.json`` says how the owner arranged their home
# page and the owner's sidebar, so it is the same on every device and the brain can
# change it when asked in chat (``arrange_dashboard`` in home_things.py).
# The browser keeps a copy only as an offline cache. Shape (every key
# optional; a missing file means the defaults below):
#
#   {"version": 1,
#    "tiles":   {"order": [tile id, ...], "sizes": {tile id: {"w": 1-12, "h": 1-12}},
#                "hidden": [tile id, ...], "pinned": [tile id, ...],
#                "names": {tile id: "her name for it"}},
#    "sidebar": {"order": [href, ...], "hidden": [href, ...],
#                "pinned": [href, ...], "names": {href: "her name for it"}},
#    "seen":    {"tiles": {tile id: name}, "sidebar": {href: label}},
#    "updated_at": "...", "updated_by": "owner" | "brain"}
#
# Tile ids: core tiles (CORE_TILES), the tiles that start hidden
# (STARTS_HIDDEN), ``<kind>:<id>`` for each installed thing (GET /installed),
# and registry widget ids (``w_...``). ``seen`` is what the page last showed,
# written by the page, so the brain can find a tile or a menu item by name.

_LAYOUT_PATH: str | None = None
LAYOUT_VERSION = 1

#: Always offered on the home page (owner names).
CORE_TILES: dict[str, str] = {"chat": "Chat", "jobs": "Build jobs",
                              "find-more": "Find more"}
#: Tiles a fresh layout keeps in the hidden tray. The owner can bring any back.
STARTS_HIDDEN: dict[str, str] = {
    "weather": "Weather", "jetson": "This computer", "smart-home": "Smart home",
    "cameras": "Cameras", "schedule": "Schedule", "reminders": "Reminders",
    "meals": "Meals", "workout": "Workout", "front": "About your bot",
    "persona": "Your bot's own card",
}
LAYOUT_ACTIONS = ("pin", "unpin", "hide", "show", "move", "rename")
_SECTIONS = ("tiles", "sidebar")
_PLACES = {"home": "tiles", "tile": "tiles", "tiles": "tiles",
           "sidebar": "sidebar", "menu": "sidebar"}
_MAX_IDS = 200
_MAX_ID = 160
_MAX_NAME = 40
_SKIP_WORDS = frozenset("the my a an tile card page menu item link sidebar home "
                        "on of".split())


def _layout_path() -> str:
    if _LAYOUT_PATH:
        return _LAYOUT_PATH
    from puffpai.config import get_state_dir
    return os.path.join(get_state_dir(), "dashboard_layout.json")


def default_layout() -> dict:
    return {"version": LAYOUT_VERSION,
            "tiles": {"order": list(CORE_TILES), "sizes": {},
                      "hidden": list(STARTS_HIDDEN), "pinned": [], "names": {}},
            "sidebar": {"order": [], "hidden": [], "pinned": [], "names": {}},
            "seen": {"tiles": {}, "sidebar": {}}}


def _ids(value) -> list[str]:
    out: list[str] = []
    for v in value if isinstance(value, list) else []:
        s = str(v).strip()[:_MAX_ID] if isinstance(v, (str, int)) else ""
        if s and s not in out:
            out.append(s)
    return out[:_MAX_IDS]


def _names(value, size: int = _MAX_NAME) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (value.items() if isinstance(value, dict) else []):
        k, v = str(k).strip()[:_MAX_ID], " ".join(str(v or "").split())[:size]
        if k and v:
            out[k] = v
        if len(out) >= _MAX_IDS:
            break
    return out


def _sizes(value) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for k, v in (value.items() if isinstance(value, dict) else []):
        try:
            w, h = int(v.get("w")), int(v.get("h"))
        except (AttributeError, TypeError, ValueError):
            continue
        out[str(k)[:_MAX_ID]] = {"w": min(12, max(1, w)), "h": min(12, max(1, h))}
        if len(out) >= _MAX_IDS:
            break
    return out


def _section(value, base: dict) -> dict:
    value = value if isinstance(value, dict) else {}
    out: dict = {k: _ids(value.get(k, base.get(k)))
                 for k in ("order", "hidden", "pinned")}
    out["names"] = _names(value.get("names", base.get("names")))
    if "sizes" in base:
        out["sizes"] = _sizes(value.get("sizes", base.get("sizes")))
    return out


def _normal(data) -> dict:
    data = data if isinstance(data, dict) else {}
    base = default_layout()
    seen = data.get("seen") if isinstance(data.get("seen"), dict) else {}
    out: dict = {"version": LAYOUT_VERSION,
                 "tiles": _section(data.get("tiles"), base["tiles"]),
                 "sidebar": _section(data.get("sidebar"), base["sidebar"]),
                 "seen": {"tiles": _names(seen.get("tiles"), 80),
                          "sidebar": _names(seen.get("sidebar"), 80)}}
    for k in ("updated_at", "updated_by"):
        if isinstance(data.get(k), str):
            out[k] = data[k][:40]
    return out


def get_layout() -> dict:
    """The saved layout, or the defaults when there is none (or it is broken)."""
    try:
        with open(_layout_path(), encoding="utf-8") as f:
            return _normal(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return _normal({})


def _write_layout(layout: dict, by: str) -> dict:
    layout = _normal(layout)
    layout["updated_at"] = _utcnow()
    layout["updated_by"] = "brain" if by == "brain" else "owner"
    path = _layout_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(layout, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return layout


def save_layout(patch: dict, by: str = "owner") -> dict:
    """``PUT /dashboard/layout``: each top-level section given (``tiles``,
    ``sidebar``, ``seen``) replaces the saved one; the rest stays."""
    patch = patch if isinstance(patch, dict) else {}
    with _lock:
        layout = get_layout()
        for key in (*_SECTIONS, "seen"):
            if isinstance(patch.get(key), dict):
                layout[key] = patch[key]
        return _write_layout(layout, by)


def _norm_words(text) -> str:
    import re
    return " ".join(w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
                    if w not in _SKIP_WORDS)


def _candidates(layout: dict, place: str) -> dict[str, str]:
    """``{id: name}`` the owner would recognize in *place* (their names win)."""
    if place == "tiles":
        known = {**CORE_TILES, **STARTS_HIDDEN, **layout["seen"]["tiles"]}
    else:
        known = dict(layout["seen"]["sidebar"])
    known.update({k: v for k, v in layout[place]["names"].items() if k in known})
    return known


def find_layout_item(layout: dict, ref, place: str = "") -> tuple[str, str, str]:
    """``(place, id, name)`` for what the owner called *ref*, or ``("", "", "")``.
    *place* is ``tiles`` or ``sidebar``; empty looks at the home page first."""
    want = _norm_words(ref)
    raw = str(ref or "").strip()
    if not want and not raw:
        return "", "", ""
    for p in [place] if place in _SECTIONS else list(_SECTIONS):
        known = _candidates(layout, p)
        if raw in known:
            return p, raw, known[raw]
        # The owner's own name for it counts as well as the one the page shows.
        rows = [(i, n, {_norm_words(n), _norm_words(i),
                        _norm_words({**CORE_TILES, **STARTS_HIDDEN,
                                     **layout["seen"][p]}.get(i, ""))})
                for i, n in known.items()]
        tests = (lambda names: want in names,
                 lambda names: bool(want) and any(set(want.split()) <= set(x.split())
                                                  for x in names if x),
                 lambda names: any(x and set(x.split()) <= set(want.split())
                                   for x in names))
        for test in tests:
            hits = [(i, n) for i, n, names in rows if test(names)]
            if hits:
                i, n = min(hits, key=lambda h: len(h[1]))
                return p, i, n
    return "", "", ""


def _full_order(layout: dict, place: str) -> list[str]:
    order = list(layout[place]["order"])
    base = list(CORE_TILES) if place == "tiles" else []
    for i in base + list(layout["seen"][place]):
        if i not in order:
            order.append(i)
    return order


def arrange(action: str, ref, place: str = "", position=None,
            name: str = "", by: str = "brain") -> dict:
    """One change to the home page or the sidebar, saved to the layout file.

    *action*: pin, unpin, hide, show, move (*position*: ``first``, ``last``
    or a number from 1), rename (*name*; empty gives the own name back).
    Returns ``{ok, line, place, id, name}``."""
    action = str(action or "").strip().lower()
    place = _PLACES.get(str(place or "").strip().lower(), "")
    if action not in LAYOUT_ACTIONS:
        return {"ok": False, "place": "", "id": "", "name": "",
                "line": "I can pin, unpin, hide, show, move or rename a tile or a "
                        "menu item."}
    with _lock:
        layout = get_layout()
        where, tid, label = find_layout_item(layout, ref, place)
        if not tid:
            names = sorted(set(_candidates(layout, "tiles").values()))[:20]
            return {"ok": False, "place": "", "id": "", "name": str(ref or ""),
                    "line": (f"I could not find \"{ref}\" on your home page or in "
                             f"the menu. On the home page: {', '.join(names)}.")}
        sec = layout[where]
        spot = "home page" if where == "tiles" else "menu"
        new_name = " ".join(str(name or "").split())[:_MAX_NAME]
        if action == "hide":
            sec["hidden"] = [*[h for h in sec["hidden"] if h != tid], tid]
            sec["pinned"] = [p for p in sec["pinned"] if p != tid]
            line = f"{label} is hidden from your {spot}. Say so and I will bring it back."
        elif action == "show":
            sec["hidden"] = [h for h in sec["hidden"] if h != tid]
            line = f"{label} is back on your {spot}."
        elif action in ("pin", "unpin"):
            sec["pinned"] = [p for p in sec["pinned"] if p != tid]
            if action == "pin":
                sec["pinned"].insert(0, tid)
                sec["hidden"] = [h for h in sec["hidden"] if h != tid]
                line = f"{label} is pinned to the top of your {spot}."
            else:
                line = f"{label} is not pinned any more."
        elif action == "move":
            order = [i for i in _full_order(layout, where) if i != tid]
            pos = str(position if position is not None else "first").strip().lower()
            if pos in ("first", "top", "start", "0", "1"):
                at = 0
            elif pos in ("last", "bottom", "end"):
                at = len(order)
            else:
                try:
                    at = min(len(order), max(0, int(float(pos)) - 1))
                except ValueError:
                    at = 0
            order.insert(at, tid)
            sec["order"] = order
            sec["hidden"] = [h for h in sec["hidden"] if h != tid]
            # Pinned things lead; "first" means before them too.
            sec["pinned"] = [p for p in sec["pinned"] if p != tid]
            if at == 0 and sec["pinned"]:
                sec["pinned"].insert(0, tid)
            line = (f"{label} is first on your {spot} now." if at == 0
                    else f"{label} is last on your {spot} now." if at == len(order) - 1
                    else f"{label} is number {at + 1} on your {spot} now.")
        else:  # rename
            if new_name:
                sec["names"][tid] = new_name
                line = f"{label} is called {new_name} on your {spot} now."
            else:
                sec["names"].pop(tid, None)
                line = f"{label} has its own name back."
        _write_layout(layout, by)
    return {"ok": True, "line": line, "place": where, "id": tid,
            "name": new_name or label}
