"""
PuffPAI Zone 3 (Bridge) — Local Telemetry Ledger
================================================

Pulls AdGuard Home's DNS query log into an append-only, per-device JSONL ledger
under ``$PUFFPAI_DATA_DIR/bridge_telemetry.jsonl`` and answers "what has this
device been trying to phone home to?" entirely from local disk.

**Nothing leaves the box.** The only network call this module makes is to the
AdGuard instance on the LAN, and it is a read (``/control/querylog``). No
domain, client address or MAC is transmitted anywhere.

**Observe-only.** The ledger records what AdGuard already saw. It does not
create filter rules, block anything, or ask AdGuard to. Whether a query was
actually refused is AdGuard's business and is reported here as ``answer``.

Rotation follows the house append-only-JSONL pattern: 4 MB per file, three
retained generations (``.jsonl.1`` .. ``.jsonl.3``), oldest discarded.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from puffpai.statedir import resolve_runtime_dir

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DATA_DIR = _REPO_ROOT / "data"

#: Rotate at 4 MB, keep three generations. Matches the append-only ledger
#: pattern used elsewhere in the tree for audit trails.
LEDGER_MAX_BYTES = 4 * 1024 * 1024
LEDGER_GENERATIONS = 3

#: Query-log rows requested from AdGuard per pull. AdGuard paginates by
#: ``older_than``; one page is enough for a 5-minute cadence and bounds the
#: memory a single pull can take.
QUERYLOG_LIMIT = 2000

#: Test/deployment override hook (house pattern). Assign a Path to pin it.
_TELEMETRY_FILE: Path | None = None

#: Test/deployment override hook for the dedup high-water-mark state file.
_LEDGER_STATE_FILE: Path | None = None

#: How long a seeded-from-disk dedup key is kept live in memory.
_RECENT_KEYS_WINDOW_SECONDS = 900

#: Cap on how many dedup keys the state file carries across a restart.
_RECENT_KEYS_CAP = 5000

#: Overlap subtracted from the persisted high-water mark before filtering
#: incoming AdGuard rows, so a row right at the seam — which the seeded
#: ``recent_keys`` already cover — is never silently dropped instead of
#: deduped.
_HIGH_WATER_OVERLAP_SECONDS = 60

#: How far ahead of "now" a row's timestamp can be and still be allowed to
#: advance the persisted high-water mark. A single clock-skewed AdGuard row
#: stamped further in the future than this is still recorded to the ledger,
#: just never used to move the mark.
_FUTURE_ROW_TOLERANCE_SECONDS = 300

#: How long a dedup key stays live in memory, measured from when the key was
#: *ingested* — not from the timestamp on the row. AdGuard hands back a rolling
#: window on every poll, so the same row is offered again and again; expiring by
#: the row's own age would retire the key while the row is still being re-served
#: and let duplicates through. An hour comfortably covers the 5-minute cadence.
_DEDUP_WINDOW_SECONDS = 3600


def _data_dir() -> Path:
    return Path(resolve_runtime_dir(str(_DEFAULT_DATA_DIR)))


def telemetry_file() -> Path:
    """Path to the append-only telemetry ledger."""
    return _TELEMETRY_FILE or _data_dir() / "bridge_telemetry.jsonl"


def ledger_state_file() -> Path:
    """Path to the persisted dedup high-water-mark state.

    Kept next to the telemetry file itself (rather than resolved
    independently through ``_data_dir()``) so that pinning ``_TELEMETRY_FILE``
    into a tmp dir — the existing test-isolation pattern — isolates this file
    too without a second override in every test.
    """
    return _LEDGER_STATE_FILE or telemetry_file().with_name(
        "bridge_ledger_state.json")


# ---------------------------------------------------------------------------
# Domain categorisation
# ---------------------------------------------------------------------------

_category_map: dict[str, str] | None = None
_category_lock = threading.Lock()


def _build_category_map() -> dict[str, str]:
    """domain -> category name, from the Zone 3 telemetry database.

    Built through :class:`~puffpai.zone3.telemetry_domains.TelemetryDB`'s public
    API only (``manufacturers`` + ``get_domains``), so the curated lists can be
    reorganised without breaking this module. ``include_common=True`` on any one
    manufacturer already folds in the cross-device tracking list.
    """
    try:
        from puffpai.zone3.telemetry_domains import TelemetryDB
    except Exception as exc:
        logger.warning("telemetry domain DB unavailable: %s", exc)
        return {}
    db = TelemetryDB()
    mapping: dict[str, str] = {}
    for manufacturer in db.manufacturers:
        for entry in db.get_domains(manufacturer, include_common=True):
            # A domain listed under two manufacturers keeps its first, more
            # specific category rather than being overwritten by the generic
            # common-tracking entry.
            mapping.setdefault(entry.domain.lower(), entry.category.value.upper())
    return mapping


def _categories() -> dict[str, str]:
    global _category_map
    if _category_map is None:
        with _category_lock:
            if _category_map is None:
                _category_map = _build_category_map()
    return _category_map


def categorize_domain(domain: str) -> str:
    """TELEMETRY | ADS | TRACKING | CLOUD | UPDATE | other.

    Matches the query name against the curated list exactly and by parent
    suffix, so ``metrics.samsungacr.com`` inherits ``samsungacr.com``.
    """
    name = (domain or "").strip().lower().rstrip(".")
    if not name:
        return "other"
    table = _categories()
    if name in table:
        return table[name]
    parts = name.split(".")
    for idx in range(1, len(parts) - 1):
        parent = ".".join(parts[idx:])
        if parent in table:
            return table[parent]
    return "other"


# ---------------------------------------------------------------------------
# AdGuard query log
# ---------------------------------------------------------------------------

def _adguard_settings() -> tuple[str, str, str, bool] | None:
    """(base_url, username, password, enforce) or None when Zone 3 is off."""
    try:
        from puffpai.config import get_config
        zone3 = getattr(get_config(), "zone3", None)
    except Exception as exc:
        logger.info("Zone 3 config unavailable: %s", exc)
        return None
    if zone3 is None or not getattr(zone3, "enabled", False):
        return None
    return (
        str(getattr(zone3, "adguard_url", "")).rstrip("/"),
        str(getattr(zone3, "adguard_username", "")),
        str(getattr(zone3, "adguard_password", "")),
        bool(getattr(zone3, "adguard_enforce", False)),
    )


def _querylog_via_http(base_url: str, username: str, password: str,
                       limit: int) -> list[dict[str, Any]]:
    """Read ``/control/querylog`` directly. Read-only GET against a LAN host.

    Why not :meth:`~puffpai.zone3.adguard_controller.AdGuardController.recent_queries`:
    that accessor exists, but it is built for a *security report*, not a ledger.
    It hard-caps ``limit`` at 500 and returns a deliberately trimmed projection
    that drops ``upstream`` — both correct choices for its own job and both
    wrong for an append-only record that has to survive a 5-minute polling gap
    and say which resolver answered. :func:`normalize_row` accepts that
    projection anyway, so switching to it later costs nothing.
    """
    if not base_url:
        return []
    try:
        from puffpai.security.lan_http import lan_get
    except Exception:
        return []
    auth = (username, password) if username else None
    try:
        # AdGuard's control API (config `zone3.adguard_url`, 127.0.0.1:3000) —
        # read-only. `lan_get` RESOLVES that configured URL and refuses any
        # address that is not loopback/RFC1918/ULA before a socket opens, so
        # "nothing leaves the house" is a check rather than a comment: an
        # edited config cannot turn this reader into an outbound call carrying
        # the household's AdGuard credential.
        resp = lan_get(f"{base_url}/control/querylog",
                       params={"limit": limit}, auth=auth, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.info("AdGuard querylog read failed: %s", exc)
        return []
    rows = payload.get("data", []) if isinstance(payload, dict) else payload
    return [row for row in (rows or []) if isinstance(row, dict)]


def fetch_querylog(limit: int = QUERYLOG_LIMIT) -> list[dict[str, Any]]:
    """Raw AdGuard query-log rows, or [] when Zone 3 is off or unreachable."""
    settings = _adguard_settings()
    if settings is None:
        return []
    base_url, username, password, _enforce = settings
    return _querylog_via_http(base_url, username, password, limit)


def _answer_of(row: dict[str, Any], enforce: bool) -> str:
    """allowed | blocked | monitor.

    AdGuard tags a filtered query with a ``reason`` beginning ``Filtered``. In
    monitor-first deployments (``zone3.adguard_enforce = False``) a "blocked"
    reason means the rule *matched* but Alex has not flipped enforcement, so
    the honest answer is ``monitor``, not ``blocked``.
    """
    reason = str(row.get("reason", ""))
    if not reason and "blocked" in row:
        # AdGuardController.recent_queries' projection: it has already made the
        # filtered/not decision and dropped the reason string.
        return ("blocked" if enforce else "monitor") if row["blocked"] else "allowed"
    filtered = reason.startswith("Filtered") or reason == "Rewrite"
    if not filtered:
        return "allowed"
    return "blocked" if enforce else "monitor"


def normalize_row(row: dict[str, Any], enforce: bool = False) -> dict[str, Any] | None:
    """One AdGuard query-log row -> one ledger entry, or None if unusable.

    Accepts both shapes: AdGuard's raw record (``question.host``) and
    ``AdGuardController.recent_queries``' projection (top-level ``domain``).
    """
    question = row.get("question") or {}
    domain = str(
        question.get("host") or question.get("name") or row.get("domain") or ""
    ).strip().lower()
    client = str(row.get("client") or "").strip()
    if not domain or not client:
        return None
    return {
        "ts": str(row.get("time") or datetime.now(timezone.utc).isoformat()),
        "client_ip": client,
        "mac": "",
        "domain": domain.rstrip("."),
        "category": categorize_domain(domain),
        "answer": _answer_of(row, enforce),
        "upstream": str(row.get("upstream") or ""),
    }


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def _normalize_iso(value: str) -> str | None:
    """Coerce an AdGuard-style ISO timestamp into something 3.10's
    ``datetime.fromisoformat`` can parse, or None if it is not salvageable.

    ``datetime.fromisoformat`` on the target interpreter (3.10.12) accepts at
    most a 6-digit fractional-second part and does not accept a trailing
    ``Z``. AdGuard hands back both a ``Z`` suffix and (from some upstreams) a
    9-digit nanosecond fraction, e.g. ``2026-09-09T19:21:31.123456789-05:00``
    without this every such row would be silently dropped. This
    normalizes:

    * surrounding whitespace, stripped
    * a trailing ``Z``/``z`` -> ``+00:00``
    * a space separator -> ``T``
    * a fractional-seconds part longer than 6 digits -> truncated to 6
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]", text):
        return None
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    if "T" not in text and " " in text:
        text = text.replace(" ", "T", 1)
    # Truncate an over-long fractional-seconds part, e.g. ".123456789" -> ".123456".
    match = re.search(r"\.(\d+)", text)
    if match and len(match.group(1)) > 6:
        start, end = match.span(1)
        text = text[:start] + match.group(1)[:6] + text[end:]
    return text


def _parse_ts(value: str) -> float | None:
    if not value:
        return None
    text = _normalize_iso(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _minute_bucket(ts: str) -> str:
    stamp = _parse_ts(ts)
    if stamp is None:
        return ts[:16]
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M")


class _InventoryKeys:
    """Per-call cache over the inventory's IP lookups.

    :meth:`BridgeInventory.key_for_ip` walks the whole roll under a lock; a
    24-hour summary can carry thousands of rows across a handful of IPs, so
    each address is resolved once. Degrades to "" — i.e. to the row's stored
    key — when the inventory cannot be reached at all.
    """

    def __init__(self) -> None:
        try:
            from puffpai.bridge_inventory import get_inventory
            self._inventory: Any = get_inventory()
        except Exception as exc:
            logger.debug("inventory unavailable for summary: %s", exc)
            self._inventory = None
        self._keys: dict[str, str] = {}
        self._macs: dict[str, str] = {}

    def _lookup(self, cache: dict[str, str], method: str, ip: str) -> str:
        if not ip or self._inventory is None:
            return ""
        if ip not in cache:
            try:
                cache[ip] = getattr(self._inventory, method)(ip) or ""
            except Exception:
                cache[ip] = ""
        return cache[ip]

    def key(self, ip: str) -> str:
        return self._lookup(self._keys, "key_for_ip", ip)

    def mac(self, ip: str) -> str:
        return self._lookup(self._macs, "mac_for_ip", ip)


class BridgeLedger:
    """Append-only local record of per-device DNS activity."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._seen: dict[tuple[str, str, str], float] = {}
        self._last_pull: str = ""
        self._last_pull_rows = 0
        self._last_written = 0
        self._high_water_ts: str = ""
        self._load_state()

    # -- dedup state persistence ----------------------------------------------

    def _load_state(self) -> None:
        """Seed the high-water mark and dedup set from a prior process's exit.

        Without this, an api restart re-offers AdGuard's whole rolling
        querylog window and every row in it duplicates in the ledger.
        """
        path = ledger_state_file()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("bridge_ledger_state.json corrupt; starting fresh")
            return
        loaded_high_water = str(raw.get("last_ingested_ts") or "")
        now = datetime.now(timezone.utc).timestamp()
        loaded_stamp = _parse_ts(loaded_high_water)
        if loaded_stamp is not None and loaded_stamp > now + 60:
            logger.warning(
                "bridge_ledger_state.json high-water mark %r is more than "
                "60s in the future; discarding it (starting with no "
                "high-water mark; seeded recent_keys still dedup the "
                "overlap)", loaded_high_water)
            loaded_high_water = ""
        self._high_water_ts = loaded_high_water
        for item in (raw.get("recent_keys") or [])[-_RECENT_KEYS_CAP:]:
            if isinstance(item, (list, tuple)) and len(item) == 3:
                self._seen[(str(item[0]), str(item[1]), str(item[2]))] = now

    def _save_state(self) -> None:
        """Write the high-water mark + recent dedup keys, atomically. Caller holds the lock."""
        path = ledger_state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).timestamp()
        cutoff = now - _RECENT_KEYS_WINDOW_SECONDS
        recent_keys = [list(key) for key, ts in self._seen.items() if ts >= cutoff]
        recent_keys = recent_keys[-_RECENT_KEYS_CAP:]
        payload = {
            "last_ingested_ts": self._high_water_ts,
            "recent_keys": recent_keys,
        }
        tmp = path.with_suffix(".json.tmp")
        # 0600: this file holds per-device dedup keys (client IP + domain).
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))
        tmp.replace(path)

    # -- rotation ------------------------------------------------------------

    def _rotate_if_needed(self) -> None:
        """4 MB cap, three generations, oldest discarded. Caller holds the lock."""
        path = telemetry_file()
        try:
            if not path.exists() or path.stat().st_size < LEDGER_MAX_BYTES:
                return
        except OSError:
            return
        oldest = path.with_suffix(f".jsonl.{LEDGER_GENERATIONS}")
        if oldest.exists():
            oldest.unlink()
        for generation in range(LEDGER_GENERATIONS - 1, 0, -1):
            src = path.with_suffix(f".jsonl.{generation}")
            if src.exists():
                src.replace(path.with_suffix(f".jsonl.{generation + 1}"))
        path.replace(path.with_suffix(".jsonl.1"))

    # -- write ---------------------------------------------------------------

    def _prune_seen(self, now: float) -> None:
        cutoff = now - _DEDUP_WINDOW_SECONDS
        for key in [k for k, ts in self._seen.items() if ts < cutoff]:
            del self._seen[key]

    def append(self, entries: Iterable[dict[str, Any]]) -> int:
        """Append entries, deduped by (client, domain, minute). Returns count written.

        A chatty device asks for the same telemetry endpoint dozens of times a
        minute; collapsing to one row per minute keeps the ledger honest about
        *when* it phoned home without letting one device's retry loop fill the
        4 MB budget.
        """
        now = datetime.now(timezone.utc).timestamp()
        fresh: list[dict[str, Any]] = []
        with self._lock:
            self._prune_seen(now)
            for entry in entries:
                if not entry:
                    continue
                key = (entry.get("client_ip", ""), entry.get("domain", ""),
                       _minute_bucket(entry.get("ts", "")))
                if key in self._seen:
                    continue
                self._seen[key] = now
                fresh.append(entry)
            if not fresh:
                return 0
            self._rotate_if_needed()
            path = telemetry_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            # 0600: append-only, but each row carries a client IP/MAC and
            # the domain it phoned home to. Rotated generations are renames
            # of this file so they inherit the mode.
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                for entry in fresh:
                    handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
        return len(fresh)

    def pull(self, limit: int = QUERYLOG_LIMIT) -> dict[str, Any]:
        """Pull AdGuard's query log once and append what is new.

        Client IPs are resolved to MACs through the inventory, so a device that
        gets a new DHCP lease still reads as the same device in the summary.

        Rows at or before ``high_water_mark - 60s`` are dropped outright: they
        were already ingested in a prior process's lifetime (AdGuard hands
        back a rolling window on every poll), and the seeded ``recent_keys``
        already cover the 60s overlap so nothing genuinely new is lost.
        """
        settings = _adguard_settings()
        if settings is None:
            return {"pulled": 0, "written": 0, "zone3": False,
                    "reason": "Zone 3 disabled"}
        enforce = settings[3]
        rows = fetch_querylog(limit)

        try:
            from puffpai.bridge_inventory import get_inventory
            inventory = get_inventory()
        except Exception:
            inventory = None

        with self._lock:
            high_water_stamp = _parse_ts(self._high_water_ts)
            newest_ts = self._high_water_ts
        newest_stamp = high_water_stamp
        now_stamp = datetime.now(timezone.utc).timestamp()

        entries: list[dict[str, Any]] = []
        for row in rows:
            entry = normalize_row(row, enforce=enforce)
            if entry is None:
                continue
            if inventory is not None:
                entry["mac"] = inventory.mac_for_ip(entry["client_ip"])
            entry_stamp = _parse_ts(entry["ts"])
            # A row more than FUTURE_ROW_TOLERANCE_SECONDS ahead of "now" is
            # ingested as-is (AdGuard saw it, so the ledger records it) but
            # must never advance the high-water mark: one clock-skewed row
            # would otherwise push the mark into the future and silence every
            # later pull (nothing newer would ever clear the ``> high_water``
            # bar), permanently, across restarts.
            if (entry_stamp is not None
                    and entry_stamp <= now_stamp + _FUTURE_ROW_TOLERANCE_SECONDS
                    and (newest_stamp is None or entry_stamp > newest_stamp)):
                newest_stamp = entry_stamp
                newest_ts = entry["ts"]
            if (high_water_stamp is not None and entry_stamp is not None
                    and entry_stamp <= high_water_stamp - _HIGH_WATER_OVERLAP_SECONDS):
                continue
            entries.append(entry)

        # Belt-and-suspenders clamp: even the "usable" newest candidate above
        # can't be trusted past "now" (e.g. a row exactly at the tolerance
        # boundary with a slightly-skewed local clock), so pin the mark we
        # persist to whichever is earlier.
        if newest_stamp is not None and newest_stamp > now_stamp:
            newest_stamp = now_stamp
            newest_ts = datetime.fromtimestamp(now_stamp, timezone.utc).isoformat()

        written = self.append(entries)
        with self._lock:
            self._last_pull = datetime.now(timezone.utc).isoformat()
            self._last_pull_rows = len(rows)
            self._last_written = written
            self._high_water_ts = newest_ts
            self._save_state()
        return {"pulled": len(rows), "written": written, "zone3": True,
                "enforce": enforce}

    # -- read ----------------------------------------------------------------

    def read(self, hours: int = 24) -> list[dict[str, Any]]:
        """Ledger entries newer than ``hours`` ago, oldest first.

        Reads the current file and, when the window reaches past its start, the
        retained generations too.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()
        base = telemetry_file()
        candidates = [base] + [
            base.with_suffix(f".jsonl.{gen}")
            for gen in range(1, LEDGER_GENERATIONS + 1)
        ]
        entries: list[dict[str, Any]] = []
        with self._lock:
            for path in candidates:
                if not path.exists():
                    continue
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    stamp = _parse_ts(entry.get("ts", ""))
                    if stamp is None or stamp < cutoff:
                        continue
                    entries.append(entry)
        entries.sort(key=lambda e: _parse_ts(e.get("ts", "")) or 0.0)
        return entries

    def summary(self, hours: int = 24) -> dict[str, Any]:
        """Per-device rollup over the window.

        Rows are grouped by resolving their ``client_ip`` through the
        **current** inventory, not by the key that was stamped on them at
        ingest time. A row written before the inventory had learned a device's
        MAC carries ``ip:<addr>`` forever (the ledger is append-only), so
        grouping on the stored key split one device — the Orin — into two
        identical entries, ``ip:192.168.50.40`` and ``00:00:5E:00:53:41``, with
        the same eleven queries counted twice.

        When the inventory knows nothing about an IP the stored key is kept,
        so a device that has since left the LAN still rolls up under the MAC
        it was recorded with rather than fragmenting by lease.
        """
        entries = self.read(hours)
        resolver = _InventoryKeys() if entries else None
        by_device: dict[str, dict[str, Any]] = {}
        domains: dict[str, Counter] = defaultdict(Counter)
        domain_category: dict[str, str] = {}

        for entry in entries:
            client_ip = entry.get("client_ip", "")
            stored_mac = entry.get("mac") or ""
            mac = (resolver.mac(client_ip) if resolver else "") or stored_mac
            key = (resolver.key(client_ip) if resolver else "") or (
                stored_mac or f"ip:{client_ip}")
            record = by_device.setdefault(key, {
                "key": key,
                "mac": mac,
                "client_ip": client_ip,
                "total_queries": 0,
                "telemetry_attempts": 0,
                "blocked": 0,
                "monitored": 0,
                "allowed": 0,
                "last_seen": "",
                "top_domains": [],
            })
            record["total_queries"] += 1
            record["client_ip"] = client_ip or record["client_ip"]
            record["mac"] = record["mac"] or mac
            category = entry.get("category", "other")
            if category in ("TELEMETRY", "ADS", "TRACKING"):
                record["telemetry_attempts"] += 1
            answer = entry.get("answer", "allowed")
            if answer in ("blocked", "monitor", "allowed"):
                record["blocked" if answer == "blocked" else
                       "monitored" if answer == "monitor" else "allowed"] += 1
            ts = entry.get("ts", "")
            # Compare by parsed instant, not by ISO-string order: "Z" and
            # "+00:00" both mean UTC but sort inconsistently as raw strings.
            ts_stamp = _parse_ts(ts)
            last_seen_stamp = _parse_ts(record["last_seen"])
            if ts_stamp is not None and (
                    last_seen_stamp is None or ts_stamp > last_seen_stamp):
                record["last_seen"] = ts
            domain = entry.get("domain", "")
            domains[key][domain] += 1
            domain_category.setdefault(domain, category)

        for key, record in by_device.items():
            record["top_domains"] = [
                {"domain": domain, "count": count,
                 "category": domain_category.get(domain, "other")}
                for domain, count in domains[key].most_common(10)
            ]

        return {
            "hours": hours,
            "entry_count": len(entries),
            "device_count": len(by_device),
            "devices": sorted(by_device.values(),
                              key=lambda d: -d["total_queries"]),
        }

    def device_summary(self, mac: str, hours: int = 24) -> dict[str, Any]:
        """The one device's rollup, keyed by MAC, inventory key, or IP.

        The identifier is also resolved through the inventory, so a device
        whose rows group under ``ip:<addr>`` is still found by its MAC (and
        vice versa) — the three names for one device all land on the one
        merged record.
        """
        wanted = (mac or "").strip()
        normalized = wanted.replace("-", ":").upper()
        aliases = {wanted, normalized, f"ip:{wanted}"}
        ips = {wanted}
        try:
            from puffpai.bridge_inventory import get_inventory
            known = get_inventory().get(wanted)
        except Exception as exc:
            logger.debug("inventory unavailable for device summary: %s", exc)
            known = None
        if known:
            aliases |= {known.get("key") or "", known.get("mac") or ""}
            if known.get("ip"):
                aliases.add(f"ip:{known['ip']}")
                ips.add(known["ip"])
        aliases.discard("")

        full = self.summary(hours)
        for record in full["devices"]:
            if record["key"] in aliases:
                return {"hours": hours, **record}
            if record["mac"] and record["mac"] in aliases:
                return {"hours": hours, **record}
            if record["client_ip"] and record["client_ip"] in ips:
                return {"hours": hours, **record}
        return {
            "hours": hours, "key": wanted, "mac": normalized, "client_ip": "",
            "total_queries": 0, "telemetry_attempts": 0, "blocked": 0,
            "monitored": 0, "allowed": 0, "last_seen": "", "top_domains": [],
        }

    def status(self) -> dict[str, Any]:
        path = telemetry_file()
        with self._lock:
            return {
                "ledger_path": str(path),
                "ledger_bytes": path.stat().st_size if path.exists() else 0,
                "last_pull": self._last_pull,
                "last_pull_rows": self._last_pull_rows,
                "last_written": self._last_written,
                "zone3_enabled": _adguard_settings() is not None,
            }


_ledger: BridgeLedger | None = None
_ledger_lock = threading.Lock()


def get_ledger() -> BridgeLedger:
    global _ledger
    if _ledger is None:
        with _ledger_lock:
            if _ledger is None:
                _ledger = BridgeLedger()
    return _ledger


def reset_ledger() -> None:
    """Drop the singleton and the category cache. Tests only."""
    global _ledger, _category_map
    with _ledger_lock:
        _ledger = None
    with _category_lock:
        _category_map = None


__all__ = [
    "BridgeLedger",
    "LEDGER_GENERATIONS",
    "LEDGER_MAX_BYTES",
    "QUERYLOG_LIMIT",
    "categorize_domain",
    "fetch_querylog",
    "get_ledger",
    "ledger_state_file",
    "normalize_row",
    "_normalize_iso",
    "reset_ledger",
    "telemetry_file",
]
