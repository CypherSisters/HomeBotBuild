"""
PuffPAI Home Blueprint — the Traffic layer
==========================================

``GET /ha/home`` draws the house. This module answers the next question the
floorplan raises: *which of these things is talking to the internet?* It joins
three things that already exist and invents no new source of truth:

* the blueprint's devices (:mod:`puffpai.home_topology`) — for ``device_id``
  and the ``bridge_key`` that ties a Home-Assistant device to a LAN row;
* the LAN inventory (:mod:`puffpai.bridge_inventory`) — for the things HA has
  never heard of (the TV, the printer, a guest's phone);
* the local telemetry ledger's **existing** per-device rollup
  (:meth:`puffpai.bridge_ledger.BridgeLedger.summary`) — for the counts. This
  module never parses the ledger file itself: one rollup, so the Drawbridge
  panel and the floorplan cannot disagree about how many queries the TV made.

Read-only, and strictly so. Nothing here writes a filter rule, changes the
zone3 mode or a bucket, appends to the ledger, or pulls the query log. The
network calls it can make are three read-only GETs of AdGuard's own API —
``/control/status``, ``/control/stats`` and ``/control/filtering/status`` (the
last from ``active_puffpai_rules()``, which :meth:`status` calls to count our
active rules) — through
:class:`~puffpai.zone3.adguard_controller.AdGuardController`, whose transport
(``lan_get``) refuses any address that is not on the LAN — so a mis-set
``adguard_url`` cannot turn this route into an outbound request.

Everything the payload carries that came off the network is **attacker
influenced**: a device chooses its own DHCP hostname and its own mDNS name, and
a domain in the query log is whatever a device asked for. A device that names
itself ``<img onerror=...>`` or ships 5 kB of ANSI escapes must not reach the
dashboard intact, and must not be able to bloat the response. So every such
string goes through :func:`clean_text`, every domain through
:func:`clean_domain` (dropped, not escaped, when it is not domain-shaped), and
every list is capped. React escapes what it renders; this is the other half of
that defence, on the side that owns the data.

The builder is pure — it takes the three sources as arguments — so the tests
can hand it a hostile house without a hub, an inventory or a ledger in sight.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: ``?hours=`` bounds. One hour is the shortest window the ledger's minute
#: buckets make meaningful; a week is as far back as it retains.
MIN_HOURS = 1
MAX_HOURS = 168
DEFAULT_HOURS = 24

#: Response caps. A floorplan has tens of things on it; these are two orders of
#: magnitude above any real house and exist so that a poisoned inventory or a
#: chatty ledger cannot make this route a memory or bandwidth problem.
MAX_DEVICE_ROWS = 1024
MAX_LAN_ONLY_ROWS = 256
MAX_TOP_DOMAINS = 8

#: Length caps for strings that came off the network.
MAX_NAME_CHARS = 64
MAX_DOMAIN_CHARS = 253          # the DNS limit, and the spec's
MAX_TIMESTAMP_CHARS = 40

#: A ``bridge_key`` is an identity, not a label: it links a Home-Assistant
#: device to a LAN row and indexes the payload. Real keys are MAC-shaped or a
#: short ``lan:``/``ip:`` id, an order of magnitude inside this cap.
MAX_KEY_CHARS = 64

#: JavaScript's ``Number.MAX_SAFE_INTEGER``. Python integers are arbitrary
#: precision and JSON numbers are not: a 401-digit count serialises fine here
#: and arrives in the browser as ``Infinity``. Every count this module emits is
#: clamped to a number a browser can actually hold.
MAX_COUNT = 2 ** 53 - 1

#: Longest digit string accepted as a count. 16 digits is already past
#: :data:`MAX_COUNT`; anything longer is not a count anybody measured.
MAX_COUNT_DIGITS = 16

#: What a domain is allowed to look like. Deliberately narrower than DNS
#: permits: ASCII letters, digits, ``-`` and ``_`` per label, labels of 1..63
#: separated by dots, no leading/trailing hyphen. Internationalised names reach
#: a resolver as punycode (``xn--``), which is ASCII, so this keeps them while
#: dropping the tricks: ``<script>``, spaces, a NUL, a right-to-left override,
#: a homoglyph smuggled in as raw Unicode. A domain that does not match is
#: DROPPED rather than escaped — the page loses one row, which is the cheap
#: failure, and nothing unparsed reaches a browser.
_DOMAIN_LABEL = r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?"
_DOMAIN_RE = re.compile(rf"^{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})*$")

#: The body of an ANSI CSI/SGR sequence — what is left of ``\x1b[31m`` once the
#: ESC itself has been dropped as a control character. Stripping only the ESC
#: turns a colour code into the visible litter ``[31m``. This is removed *only*
#: from strings that actually carried an ESC, so ordinary bracketed text like
#: ``Lamp [kitchen]`` is never touched.
_CSI_BODY_RE = re.compile(r"\[[0-9;?]*[A-Za-z]")


# ---------------------------------------------------------------------------
# Sanitisers
# ---------------------------------------------------------------------------

def clean_text(value: Any, limit: int = MAX_NAME_CHARS) -> str:
    """A display string safe to hand a browser: no control characters, capped.

    Drops every Unicode character in the C (control/format/surrogate/private)
    and Z-line/paragraph categories — that is C0, C1, DEL, the bidi overrides,
    zero-width joiners and the rest — then collapses surrounding whitespace and
    truncates. Truncation is by character, after stripping, so the cap is a cap
    on what is rendered rather than on what was sent.

    Dropping the ESC of an ANSI escape sequence leaves its body behind
    (``\\x1b[31m`` -> ``[31m``), so when the original carried an ESC the
    orphaned CSI bodies go too — see :data:`_CSI_BODY_RE`.
    """
    if value is None or isinstance(value, bool):
        return ""
    if not isinstance(value, str):
        value = str(value)
    kept = []
    for char in value:
        category = unicodedata.category(char)
        if category[0] == "C" or category in ("Zl", "Zp"):
            continue
        kept.append(char)
    text = "".join(kept)
    if "\x1b" in value:
        text = _CSI_BODY_RE.sub("", text)
    return text.strip()[:max(0, limit)]


def clean_domain(value: Any) -> str:
    """A lowercased domain, or ``""`` when it is not one. Never raises.

    A domain is NOT put through :func:`clean_text` first. Stripping an invisible
    character out of ``exa<ZWSP>mple.com`` would leave ``example.com`` — a
    different, more trustworthy-looking name than the one that was actually
    looked up, printed next to a count that belongs to the other one. So any
    control or format character anywhere in the value **drops the row**: the
    page loses one line, which is the cheap failure, and it never shows the
    household a domain nobody asked for.

    A non-string is not a domain and is never coerced into one: ``123`` is an
    integer somebody put in the wrong field, and printing ``"123"`` as a domain
    the household's TV looked up would be a made-up fact.
    """
    if not isinstance(value, str):
        return ""
    text = value
    for char in text:
        category = unicodedata.category(char)
        if category[0] == "C" or category in ("Zl", "Zp"):
            return ""
    text = text.strip()
    if not text:
        return ""
    text = text.rstrip(".").lower()
    if not text or len(text) > MAX_DOMAIN_CHARS:
        return ""
    if not _DOMAIN_RE.match(text):
        return ""
    return text


def clean_ip(value: Any) -> str:
    """The address, or ``""``. Parsed, so nothing shaped like an address slips
    through pretending to be one."""
    text = clean_text(value, 64)
    if not text:
        return ""
    try:
        return str(ipaddress.ip_address(text.split("%", 1)[0]))
    except ValueError:
        return ""


def clean_key(value: Any) -> str:
    """A usable ``bridge_key``, or ``""`` when it cannot be one. Never raises.

    The key is an identity: it links a device to its ledger row and indexes the
    payload. It is therefore never truncated — capping two different 200-char
    keys at 64 would merge two devices' traffic under one row, which is a
    wrong answer rather than a missing one. A key that does not fit
    :data:`MAX_KEY_CHARS` DROPS its row: the page loses a device, which is the
    cheap failure.
    """
    text = clean_text(value, MAX_KEY_CHARS + 1)
    if not text or len(text) > MAX_KEY_CHARS:
        return ""
    return text


def _count(value: Any) -> int:
    """A non-negative int a browser can hold, from whatever the rollup carried.

    * ``bool`` is not a count (``True`` is not one query).
    * ``int`` is clamped to :data:`MAX_COUNT`. Python has no integer ceiling;
      JSON's consumer does, and ``10 ** 400`` reaches JavaScript as
      ``Infinity`` — which then renders and sorts as a number larger than every
      real device's traffic.
    * ``float`` only when finite, floored, then clamped. ``nan``/``inf`` are
      not counts.
    * ``str`` only when it is 1..16 ASCII digits. A JSON rollup can legitimately
      carry a count as a string, and reading ``"12"`` as ``0`` would silently
      zero a device — and, on ``entry_count``, flip ``seeing_traffic`` to false
      on a harmless shape change, which is the one field that must not lie.
      Anything else — a sign, a space, ``1e3``, Arabic-Indic digits — is 0.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, str):
        if (len(value) > MAX_COUNT_DIGITS or not value.isascii()
                or not value.isdigit()):
            return 0
        return min(int(value), MAX_COUNT)
    if isinstance(value, float):
        if not math.isfinite(value):
            return 0
        try:
            value = math.floor(value)
        except (ValueError, OverflowError):
            return 0
    elif not isinstance(value, int):
        return 0
    if value <= 0:
        return 0
    return min(value, MAX_COUNT)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def _top_domains(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The rollup's ``top_domains``, sanitised and capped.

    NOTE on shape: the ledger's rollup carries ``{domain, count, category}``
    where *category* is the traffic class it assigned (``TELEMETRY``, ``ADS``,
    ``TRACKING``, ``other``…). It does **not** record a per-domain
    allowed/blocked/monitored verdict — that verdict exists only per device, in
    the row's own ``allowed``/``blocked``/``monitored`` counts. Rather than
    invent a status this data cannot support, the category is emitted as the
    third field and the honest per-domain answer is "we know what kind of
    domain it is, not how each lookup ended".
    """
    raw = record.get("top_domains")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        domain = clean_domain(item.get("domain"))
        if not domain:
            continue
        out.append({
            "domain": domain,
            "count": _count(item.get("count")),
            "category": clean_text(item.get("category"), 32) or "other",
        })
        if len(out) >= MAX_TOP_DOMAINS:
            break
    return out


def _traffic_row(bridge_key: str, record: dict[str, Any] | None,
                 fallback_ip: str = "") -> dict[str, Any]:
    """One device's counts. A device with no ledger rows is zeros, not absent:
    "this thing made no queries in the window" is the answer the page needs."""
    record = record if isinstance(record, dict) else {}
    allowed = _count(record.get("allowed"))
    blocked = _count(record.get("blocked"))
    monitored = _count(record.get("monitored"))
    total = _count(record.get("total_queries"))
    return {
        # Already through clean_key(): emitted whole, never truncated, so the
        # key the page indexes by is the key the join was made on.
        "bridge_key": clean_key(bridge_key),
        "ip": clean_ip(record.get("client_ip")) or clean_ip(fallback_ip),
        "allowed": allowed,
        "blocked": blocked,
        "monitored": monitored,
        # The rollup counts every entry, including answers it could not
        # classify, so total is its own number and not the sum of three.
        "total": total,
        "last_seen": clean_text(record.get("last_seen"), MAX_TIMESTAMP_CHARS),
        "top_domains": _top_domains(record),
    }


def _index(ledger_summary: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """``key -> rollup record``, with the record's MAC as an extra alias.

    The blueprint's ``bridge_key`` is the inventory key, which is a MAC when
    one is known and ``ip:<addr>`` otherwise; the ledger groups by the same
    resolver. The MAC alias covers a row recorded before the inventory learned
    the device's key. An alias never overwrites a real key.
    """
    records: dict[str, dict[str, Any]] = {}
    rows = (ledger_summary or {}).get("devices")
    if not isinstance(rows, list):
        return records
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "")
        if key:
            records[key] = row
    for row in rows:
        if not isinstance(row, dict):
            continue
        mac = str(row.get("mac") or "")
        if mac and mac not in records:
            records[mac] = row
    return records


# ---------------------------------------------------------------------------
# The builder — pure
# ---------------------------------------------------------------------------

def build_traffic(*, hours: int = DEFAULT_HOURS,
                  home_devices: Iterable[Any] = (),
                  inventory: Iterable[Any] = (),
                  ledger_summary: dict[str, Any] | None = None,
                  adguard: dict[str, Any] | None = None,
                  posture: dict[str, Any] | None = None,
                  generated_at: str | None = None) -> dict[str, Any]:
    """The whole ``/ha/home/traffic`` body. A function of its arguments only.

    *home_devices* are ``/ha/home``'s device dicts, *inventory* the LAN rows
    (``bridge_inventory`` ``to_dict()`` shape), *ledger_summary* the output of
    ``BridgeLedger.summary(hours)``, *adguard* AdGuard's own status
    (``reachable``/``protection_enabled``/``queries_today``) or ``None`` when
    it could not be reached, *posture* ``{"enforcing", "mode"}``.
    """
    records = _index(ledger_summary)
    adguard = adguard if isinstance(adguard, dict) else {}
    posture = posture if isinstance(posture, dict) else {}

    inventory_ips: dict[str, str] = {}
    inventory_rows: list[dict[str, Any]] = []
    for row in inventory:
        if not isinstance(row, dict):
            continue
        key = clean_key(row.get("key"))
        if not key:
            continue                     # unusable key => drop, never truncate
        inventory_rows.append((key, row))
        inventory_ips[key] = str(row.get("ip") or "")

    devices: dict[str, dict[str, Any]] = {}
    linked_keys: set[str] = set()
    for device in home_devices:
        if not isinstance(device, dict):
            continue
        bridge_key = clean_key(device.get("bridge_key"))
        device_id = str(device.get("device_id") or "")
        if not bridge_key or not device_id:
            continue                     # no usable LAN row => nothing to say
        linked_keys.add(bridge_key)
        if len(devices) >= MAX_DEVICE_ROWS:
            continue
        devices[device_id] = _traffic_row(
            bridge_key, records.get(bridge_key),
            inventory_ips.get(bridge_key, ""))

    lan_only: list[dict[str, Any]] = []
    for key, row in inventory_rows:
        if key in linked_keys:
            continue
        entry = _traffic_row(key, records.get(key), str(row.get("ip") or ""))
        entry.update({
            "key": entry["bridge_key"],
            "display_name": clean_text(row.get("display_name")),
            "category": clean_text(row.get("category"), 32) or "unknown",
            "manufacturer": clean_text(row.get("manufacturer")),
            "hostname": clean_text(row.get("hostname")),
            "is_gateway": bool(row.get("is_gateway")),
            "is_self": bool(row.get("is_self")),
        })
        lan_only.append(entry)
    # Busiest first, then by key so the order is stable across polls.
    lan_only.sort(key=lambda r: (-r["total"], r["key"]))
    del lan_only[MAX_LAN_ONLY_ROWS:]

    queries_today = _count(adguard.get("queries_today"))
    entry_count = _count((ledger_summary or {}).get("entry_count"))
    return {
        "success": True,
        "hours": int(hours),
        "generated_at": generated_at or datetime.now(
            timezone.utc).isoformat(timespec="seconds"),
        "visibility": {
            "adguard_reachable": bool(adguard.get("reachable")),
            "queries_today": queries_today,
            "protection_enabled": bool(adguard.get("protection_enabled")),
            "enforcing": bool(posture.get("enforcing")),
            "mode": clean_text(posture.get("mode"), 16) or "observe",
            # "Is this layer telling the truth right now?" Either the ledger
            # holds something in the window or AdGuard counted queries today.
            # A house with neither gets an honest "we see nothing" instead of
            # an all-quiet map that looks like proof of no traffic.
            "seeing_traffic": entry_count >= 1 or queries_today > 0,
        },
        "devices": devices,
        "lan_only": lan_only,
    }


# ---------------------------------------------------------------------------
# I/O — every source guarded, because a missing one is a zero, not a 500
# ---------------------------------------------------------------------------

def parse_hours(raw: Any, default: int = DEFAULT_HOURS) -> int | None:
    """``hours`` as an int in 1..168, or ``None`` for "reject this request".

    Deliberately stricter than ``/bridge``'s ``_hours()``, which silently reads
    a bad value as 24: a window is the one parameter this route has, and a page
    that asked for 4 hours and was quietly given 24 would draw the wrong
    picture of the house's traffic under a heading that says otherwise.
    """
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return None
    text = clean_text(raw, 32)
    # ASCII digits only. ``int()`` happily reads Arabic-Indic and other Unicode
    # decimal digits, which is a needless second spelling for one parameter.
    if not re.fullmatch(r"[+-]?[0-9]{1,9}", text):
        return None
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    if value < MIN_HOURS or value > MAX_HOURS:
        return None
    return value


def _home_devices() -> list[dict[str, Any]]:
    """The blueprint's devices, built from the cached registries.

    Uses :func:`puffpai.home_topology.build_home` with no states: the traffic
    layer needs ``device_id`` and ``bridge_key``, neither of which comes from a
    state, so no entity states are ever fetched. Home Assistant is not wholly
    untouched, though: on a cold cache ``registry_snapshot()`` makes one
    read-only registry pull. A warm cache makes no call at all, and nothing on
    this path writes. ``include_system`` stays false so the keys line up with
    the floorplan the page is drawing.
    """
    try:
        from puffpai import home_topology
        snapshot = home_topology.registry_snapshot()
        body = home_topology.build_home(
            devices=snapshot["devices"], entities=snapshot["entities"],
            areas=snapshot["areas"], floors=snapshot["floors"],
            layout=None, links=home_topology._inventory_links(),
        )
        return list(body.get("devices") or [])
    except Exception as exc:
        logger.info("home devices unavailable for traffic: %s", exc)
        return []


def _inventory_rows() -> list[dict[str, Any]]:
    try:
        from puffpai.bridge_inventory import get_inventory
        return list(get_inventory().devices())
    except Exception as exc:
        logger.info("inventory unavailable for traffic: %s", exc)
        return []


def _ledger_summary(hours: int) -> dict[str, Any]:
    """The existing rollup. A failure is an empty window, never an exception."""
    try:
        from puffpai.bridge_ledger import get_ledger
        return get_ledger().summary(hours)
    except Exception as exc:
        logger.info("telemetry rollup unavailable for traffic: %s", exc)
        return {}


def _adguard_status() -> dict[str, Any] | None:
    """AdGuard's own status, or ``None``. Three read-only GETs, no writes.

    The controller is constructed with ``enforce=False`` and only
    :meth:`status` is called, which GETs ``/status`` and ``/stats`` and then
    counts our active rules via ``active_puffpai_rules()`` — a GET of
    ``/filtering/status``. Nothing on this path can write a rule. Its
    transport refuses non-LAN addresses, so this cannot become an outbound
    request even if the configured URL is wrong.
    """
    try:
        from puffpai.bridge_ledger import _adguard_settings
        settings = _adguard_settings()
        if not settings:
            return None
        base_url, username, password, _enforce = settings
        if not base_url:
            return None
        from puffpai.zone3.adguard_controller import AdGuardController
        status = AdGuardController(base_url, username, password,
                                   enforce=False).status()
    except Exception as exc:
        logger.info("AdGuard status unavailable for traffic: %s", exc)
        return None
    if not getattr(status, "reachable", False):
        return None
    return {
        "reachable": True,
        "protection_enabled": bool(status.protection_enabled),
        "queries_today": _count(getattr(status, "queries_today", 0)),
    }


def _posture() -> dict[str, Any]:
    """``{enforcing, mode}`` read off the config, the way /bridge/status does."""
    enabled = False
    enforce = False
    try:
        from puffpai.config import get_config
        zone3 = getattr(get_config(), "zone3", None)
        enabled = bool(getattr(zone3, "enabled", False))
        enforce = bool(getattr(zone3, "adguard_enforce", False))
    except Exception as exc:
        logger.info("zone3 posture unavailable for traffic: %s", exc)
    enforcing = enabled and enforce
    return {"enforcing": enforcing, "mode": "enforce" if enforcing else "observe"}


def traffic_payload(hours: int = DEFAULT_HOURS) -> dict[str, Any]:
    """The route's body. Gathers the four sources, then builds. Never raises
    for a source being down: that is ``adguard_reachable=false`` and zeros."""
    return build_traffic(
        hours=hours,
        home_devices=_home_devices(),
        inventory=_inventory_rows(),
        ledger_summary=_ledger_summary(hours),
        adguard=_adguard_status(),
        posture=_posture(),
    )


__all__ = [
    "DEFAULT_HOURS", "MIN_HOURS", "MAX_HOURS",
    "MAX_DEVICE_ROWS", "MAX_LAN_ONLY_ROWS", "MAX_TOP_DOMAINS",
    "MAX_NAME_CHARS", "MAX_DOMAIN_CHARS", "MAX_KEY_CHARS", "MAX_COUNT",
    "build_traffic", "clean_domain", "clean_ip", "clean_key", "clean_text",
    "parse_hours", "traffic_payload",
]
