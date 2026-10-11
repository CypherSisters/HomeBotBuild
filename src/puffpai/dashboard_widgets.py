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
        "description": "Timers & Alarms — active countdowns at a glance. "
                       "Reads live from app_data/alarmflow.json.",
    },
    "camera": {
        "title": "Security Camera",
        "consent_class": "device",
        "props": {"entity_id": "HA camera entity"},
        "description": "Live camera tile — add via dashboard_add_camera "
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


def list_widgets(include_cameras: bool = False) -> list[dict]:
    items = list(_load())
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
        return False, ("Cameras are Sentinel-gated — use dashboard_add_camera "
                       "(or POST /dashboard/cameras) instead."), None

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
                    or (wtype in ("calendar", "now-playing", "alarmflow"))):
                return False, (f"A {wtype} widget"
                               f"{' for that feed' if wtype == 'news' else ''} "
                               f"is already on the dashboard "
                               f"('{w['props'].get('title')}')."), w
        items.append(widget)
        _save(items)
    return True, (f"Done — '{props.get('title', wtype)}' ({wtype} widget) is "
                  f"now on the dashboard."), widget


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
    key = f"{lat:.3f},{lon:.3f}"
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
                    "current": "relative_humidity_2m",
                    "temperature_unit": "fahrenheit", "timezone": "auto",
                    "forecast_days": 1},
            timeout=8).json()
        out["high"] = round(f["daily"]["temperature_2m_max"][0])
        out["low"] = round(f["daily"]["temperature_2m_min"][0])
        try:
            precip = (f["daily"].get("precipitation_sum") or [None])[0]
            rh = f.get("current", {}).get("relative_humidity_2m")
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
    # A door-closed result is NOT cached: flipping the switch on in Settings
    # must take effect on the next poll, not 30 minutes later.
    if out and "blocked" not in out:
        _env_cache[key] = (now, out)
    return out


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
