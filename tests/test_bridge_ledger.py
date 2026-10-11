"""Tests for puffpai.bridge_ledger — the local, per-device telemetry ledger.

AdGuard is never contacted: every test either injects rows directly or stubs
:func:`puffpai.bridge_ledger.fetch_querylog`.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from puffpai import bridge_ledger as bl


def _iso(minutes_ago: int = 0, seconds: int = 0) -> str:
    stamp = (datetime.now(timezone.utc)
             - timedelta(minutes=minutes_ago, seconds=seconds))
    return stamp.isoformat()


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(bl, "_TELEMETRY_FILE", tmp_path / "bridge_telemetry.jsonl")
    monkeypatch.setattr(bl, "_LEDGER_STATE_FILE", tmp_path / "bridge_ledger_state.json")
    bl.reset_ledger()
    yield
    bl.reset_ledger()


@pytest.fixture
def zone3_on(monkeypatch):
    """Zone 3 enabled, monitor-first (enforce off) — the deployed posture."""
    monkeypatch.setattr(
        bl, "_adguard_settings",
        lambda: ("http://192.168.50.140:3000", "", "", False))


def _entry(client="192.168.1.20", domain="samsungacr.com", ts=None,
           answer="monitor", category=None, mac=""):
    return {
        "ts": ts or _iso(),
        "client_ip": client,
        "mac": mac,
        "domain": domain,
        "category": category or bl.categorize_domain(domain),
        "answer": answer,
        "upstream": "https://dns.local",
    }


# ── categorisation ──────────────────────────────────────────────────────────

def test_categorize_known_telemetry_domain():
    assert bl.categorize_domain("samsungacr.com") == "TELEMETRY"


def test_categorize_matches_parent_suffix():
    # A subdomain inherits its parent's curated category.
    assert bl.categorize_domain("metrics.samsungacr.com") == "TELEMETRY"
    assert bl.categorize_domain("a.b.samsungacr.com") == "TELEMETRY"


def test_categorize_is_case_and_dot_insensitive():
    assert bl.categorize_domain("SamsungACR.COM.") == "TELEMETRY"


def test_categorize_unknown_is_other():
    assert bl.categorize_domain("puffpai.com") == "other"
    assert bl.categorize_domain("") == "other"


def test_category_values_are_the_documented_set():
    values = set(bl._categories().values())
    assert values <= {"TELEMETRY", "ADS", "TRACKING", "CLOUD", "UPDATE"}
    assert "TELEMETRY" in values


def test_categorize_does_not_match_a_lookalike_suffix():
    # "notsamsungacr.com" must not inherit "samsungacr.com".
    assert bl.categorize_domain("notsamsungacr.com") == "other"


# ── normalisation ───────────────────────────────────────────────────────────

RAW = {
    "time": "2026-09-09T10:00:00.123Z",
    "client": "192.168.1.20",
    "question": {"host": "samsungacr.com", "type": "A"},
    "reason": "FilteredBlackList",
    "upstream": "https://dns.quad9.net",
}


def test_normalize_row_monitor_mode():
    entry = bl.normalize_row(RAW, enforce=False)
    assert entry["domain"] == "samsungacr.com"
    assert entry["client_ip"] == "192.168.1.20"
    assert entry["category"] == "TELEMETRY"
    # Rule matched but Alex has not flipped enforcement: "monitor", not
    # "blocked". Saying "blocked" here would be a lie about what happened.
    assert entry["answer"] == "monitor"
    assert entry["upstream"] == "https://dns.quad9.net"


def test_normalize_row_enforce_mode():
    assert bl.normalize_row(RAW, enforce=True)["answer"] == "blocked"


def test_normalize_row_unfiltered_is_allowed():
    row = dict(RAW, reason="NotFilteredNotFound")
    assert bl.normalize_row(row, enforce=True)["answer"] == "allowed"


def test_normalize_row_rejects_unusable_rows():
    assert bl.normalize_row({"client": "192.168.1.20"}) is None
    assert bl.normalize_row({"question": {"host": "x.com"}}) is None


# ── H1: timestamp normalization (3.10 fromisoformat is strict) ─────────────

def test_normalize_iso_truncates_9_digit_fraction_and_z():
    assert (bl._normalize_iso("2026-09-09T19:21:31.123456789Z")
            == "2026-09-09T19:21:31.123456+00:00")


def test_normalize_iso_9_digit_fraction_with_offset_parses_to_right_instant():
    text = bl._normalize_iso("2026-09-09T19:21:31.123456789-05:00")
    assert text == "2026-09-09T19:21:31.123456-05:00"
    stamp = bl._parse_ts("2026-09-09T19:21:31.123456789-05:00")
    assert stamp is not None
    # -05:00 means this instant is 2026-09-10T00:21:31.123456+00:00.
    expected = datetime(2026, 9, 10, 0, 21, 31, 123456, tzinfo=timezone.utc)
    assert stamp == pytest.approx(expected.timestamp())


def test_normalize_iso_z_suffix_parses():
    stamp = bl._parse_ts("2026-09-09T19:21:31Z")
    expected = datetime(2026, 9, 9, 19, 21, 31, tzinfo=timezone.utc)
    assert stamp == pytest.approx(expected.timestamp())


def test_normalize_iso_garbage_is_none():
    assert bl._normalize_iso("garbage") is None
    assert bl._parse_ts("garbage") is None


def test_normalize_iso_empty_is_none():
    assert bl._normalize_iso("") is None
    assert bl._normalize_iso(None) is None


def test_normalize_row_on_realistic_adguard_row_with_9_digit_fraction():
    # A real AdGuard querylog row shape: "time" at top level, "question.name"
    # (not "host"), a 9-digit fractional-seconds timestamp with an offset.
    row = {
        "time": "2026-09-09T19:21:31.987654321-05:00",
        "client": "192.168.50.70",
        "question": {"name": "samsungads.com"},
        "reason": "NotFilteredNotFound",
        "upstream": "https://dns.quad9.net:443/dns-query",
    }
    entry = bl.normalize_row(row, enforce=False)
    assert entry is not None
    assert entry["domain"] == "samsungads.com"
    assert entry["client_ip"] == "192.168.50.70"
    assert bl._normalize_iso(entry["ts"]) == \
        "2026-09-09T19:21:31.987654-05:00"
    assert bl._parse_ts(entry["ts"]) is not None


# ── append + dedup ──────────────────────────────────────────────────────────

def test_append_writes_jsonl():
    ledger = bl.get_ledger()
    assert ledger.append([_entry(), _entry(domain="samsungads.com")]) == 2
    lines = bl.telemetry_file().read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["domain"] == "samsungacr.com"


def test_dedup_by_client_domain_minute():
    ledger = bl.get_ledger()
    ts = "2026-09-09T10:00:05+00:00"
    same_minute = "2026-09-09T10:00:59+00:00"
    next_minute = "2026-09-09T10:01:00+00:00"
    assert ledger.append([_entry(ts=ts)]) == 1
    assert ledger.append([_entry(ts=same_minute)]) == 0     # collapsed
    assert ledger.append([_entry(ts=next_minute)]) == 1     # new minute
    assert ledger.append([_entry(ts=ts, client="192.168.1.21")]) == 1
    assert ledger.append([_entry(ts=ts, domain="samsungads.com")]) == 1


def test_dedup_collapses_within_one_batch():
    ledger = bl.get_ledger()
    ts = "2026-09-09T10:00:05+00:00"
    assert ledger.append([_entry(ts=ts)] * 50) == 1


def test_append_of_nothing_writes_nothing():
    assert bl.get_ledger().append([]) == 0
    assert not bl.telemetry_file().exists()


# ── rotation ────────────────────────────────────────────────────────────────

def _rotate_with_marker(ledger, path, gen_marker):
    """Force exactly one rotation with a single, uniquely-tagged row.

    ``LEDGER_MAX_BYTES`` is dropped to 1 so the very next append rotates
    whatever is currently in the live file, then restored so the marker row
    itself lands in a fresh, un-rotated live file.
    """
    import puffpai.bridge_ledger as bl_mod
    old = bl_mod.LEDGER_MAX_BYTES
    bl_mod.LEDGER_MAX_BYTES = 1
    try:
        ledger.append([_entry(domain=f"marker{gen_marker}.example.com",
                              ts=_iso(), answer="allowed")])
    finally:
        bl_mod.LEDGER_MAX_BYTES = old


def _markers_in(path) -> set:
    if not path.exists():
        return set()
    found = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        domain = json.loads(line).get("domain", "")
        if domain.startswith("marker") and domain.endswith(".example.com"):
            found.add(int(domain[len("marker"):-len(".example.com")]))
    return found


def test_rotation_discards_the_oldest_generation(monkeypatch):
    """Rotate N+2 times with a distinct marker each time and check the seam.

    A vacuous version of this test only checks that ``.jsonl.1..N`` exist,
    which passes even if rotation silently keeps stale content or never
    actually discards anything. This version plants a unique, identifiable
    row per rotation and checks *which* rows ended up *where* — including
    that the two oldest are gone from every retained file.
    """
    ledger = bl.get_ledger()
    path = bl.telemetry_file()

    # N+3 calls => N+2 actual rotation events (the first call finds no live
    # file to rotate), which is exactly enough for two markers to fall off
    # the retained N+1 slots (live + .1..N) entirely.
    total_calls = bl.LEDGER_GENERATIONS + 3
    for gen in range(total_calls):
        _rotate_with_marker(ledger, path, gen)

    # (a) exactly generations 1..N exist, N+1 does not.
    for gen in range(1, bl.LEDGER_GENERATIONS + 1):
        assert path.with_suffix(f".jsonl.{gen}").exists(), f"missing gen {gen}"
    assert not path.with_suffix(f".jsonl.{bl.LEDGER_GENERATIONS + 1}").exists()

    # (b) .jsonl.1 holds the newest-rotated marker; .jsonl.N holds the
    #     marker from N rotations ago.
    newest_rotated = total_calls - 2
    oldest_retained = total_calls - 1 - bl.LEDGER_GENERATIONS
    assert newest_rotated in _markers_in(path.with_suffix(".jsonl.1"))
    assert oldest_retained in _markers_in(
        path.with_suffix(f".jsonl.{bl.LEDGER_GENERATIONS}"))

    # (c) the two oldest markers appear in no file at all, live included.
    all_markers: set = set()
    for gen in range(0, bl.LEDGER_GENERATIONS + 1):
        suffix = ".jsonl" if gen == 0 else f".jsonl.{gen}"
        all_markers |= _markers_in(path.with_suffix(suffix))
    # Every marker older than the one retained in the last generation must be
    # gone entirely — for N+3 calls that is exactly the two oldest.
    for discarded in range(oldest_retained):
        assert discarded not in all_markers


def test_rotation_discards_a_stale_orphaned_oldest_generation(monkeypatch, tmp_path):
    """A pre-existing ``.jsonl.N`` with no ``.jsonl.(N-1)`` behind it must go.

    This is the case the explicit ``oldest.unlink()`` step exists for: when
    the generation chain is not fully populated, nothing in the shift loop
    ever touches ``.jsonl.N``, so a stale/orphaned oldest file would survive
    forever without an explicit delete. (In the common fully-populated case
    the shift loop's own ``replace()`` happens to overwrite it too, which is
    what made the original test vacuous — it never exercised this path.)
    """
    ledger = bl.get_ledger()
    path = bl.telemetry_file()
    stale = path.with_suffix(f".jsonl.{bl.LEDGER_GENERATIONS}")
    stale.write_text('{"domain": "STALE_ORPHAN"}\n', encoding="utf-8")
    assert not path.with_suffix(f".jsonl.{bl.LEDGER_GENERATIONS - 1}").exists()
    # Something already live, so the next append finds a file to rotate.
    path.write_text('{"domain": "old-live.example.com"}\n', encoding="utf-8")

    monkeypatch.setattr(bl, "LEDGER_MAX_BYTES", 1)
    ledger.append([_entry(domain="fresh.example.com", ts=_iso())])

    # The stale file is gone outright (nothing shifted into it, since .2
    # never existed) — either way, its content must not survive.
    assert not stale.exists() or "STALE_ORPHAN" not in stale.read_text(
        encoding="utf-8")


def test_read_spans_rotated_generations(monkeypatch):
    ledger = bl.get_ledger()
    for minute in range(5):
        ledger.append([_entry(ts=_iso(minutes_ago=minute))])
    # Force exactly one rotation, the way a 4 MB overflow would, then keep
    # writing: read() must see both sides of the seam.
    monkeypatch.setattr(bl, "LEDGER_MAX_BYTES", 1)
    ledger.append([_entry(ts=_iso(minutes_ago=10))])
    monkeypatch.setattr(bl, "LEDGER_MAX_BYTES", 4 * 1024 * 1024)
    for minute in range(20, 25):
        ledger.append([_entry(ts=_iso(minutes_ago=minute))])
    assert bl.telemetry_file().with_suffix(".jsonl.1").exists()
    assert len(ledger.read(hours=24)) == 11


def test_read_honours_the_window():
    ledger = bl.get_ledger()
    ledger.append([_entry(ts=_iso(minutes_ago=10))])
    ledger.append([_entry(ts=_iso(minutes_ago=60 * 40), domain="samsungads.com")])
    assert len(ledger.read(hours=24)) == 1
    assert len(ledger.read(hours=48)) == 2


def test_read_skips_corrupt_lines():
    ledger = bl.get_ledger()
    ledger.append([_entry()])
    with bl.telemetry_file().open("a", encoding="utf-8") as handle:
        handle.write("{ not json\n\n")
    assert len(ledger.read(hours=24)) == 1


# ── summary ─────────────────────────────────────────────────────────────────

def _populate(ledger):
    ledger.append([
        _entry(domain="samsungacr.com", ts=_iso(minutes_ago=1),
               mac="AA:BB:CC:00:00:01", answer="monitor"),
        _entry(domain="samsungacr.com", ts=_iso(minutes_ago=2),
               mac="AA:BB:CC:00:00:01", answer="monitor"),
        _entry(domain="samsungads.com", ts=_iso(minutes_ago=3),
               mac="AA:BB:CC:00:00:01", answer="blocked"),
        _entry(domain="puffpai.com", ts=_iso(minutes_ago=4),
               mac="AA:BB:CC:00:00:01", answer="allowed"),
        _entry(client="192.168.1.30", domain="puffpai.com",
               ts=_iso(minutes_ago=5), answer="allowed"),
    ])


def test_summary_rolls_up_per_device():
    ledger = bl.get_ledger()
    _populate(ledger)
    summary = ledger.summary(hours=24)

    assert summary["hours"] == 24
    assert summary["entry_count"] == 5
    assert summary["device_count"] == 2

    tv = summary["devices"][0]                      # sorted by total, desc
    assert tv["key"] == "AA:BB:CC:00:00:01"
    assert tv["total_queries"] == 4
    assert tv["telemetry_attempts"] == 3            # TELEMETRY + ADS
    assert tv["blocked"] == 1
    assert tv["monitored"] == 2
    assert tv["allowed"] == 1

    other = summary["devices"][1]
    assert other["key"] == "ip:192.168.1.30"        # no MAC resolved
    assert other["total_queries"] == 1


def test_summary_top_domains_capped_at_ten_with_categories():
    ledger = bl.get_ledger()
    entries = []
    for idx in range(15):
        for minute in range(idx + 1):
            entries.append(_entry(domain=f"d{idx:02d}.example.com",
                                  ts=f"2026-09-09T10:{minute:02d}:00+00:00"))
    entries.append(_entry(domain="samsungacr.com",
                          ts="2026-09-09T09:00:00+00:00"))
    ledger.append(entries)
    summary = ledger.summary(hours=24 * 7)
    top = summary["devices"][0]["top_domains"]
    assert len(top) == 10
    assert top[0]["domain"] == "d14.example.com"    # most frequent first
    assert top[0]["count"] == 15
    assert all("category" in row for row in top)
    assert {row["category"] for row in top} == {"other"}


def test_summary_of_an_empty_ledger():
    summary = bl.get_ledger().summary(hours=24)
    assert summary == {"hours": 24, "entry_count": 0, "device_count": 0,
                       "devices": []}


def test_summary_last_seen_compares_by_time_not_iso_string():
    """Two stamps where raw ISO-string order and real time order disagree.

    "2026-09-09T23:00:00+05:00" (18:00 UTC) sorts AFTER
    "2026-09-09T20:00:00Z" (20:00 UTC) as a plain string — "23" > "20" — even
    though it is actually the EARLIER instant (18:00 UTC < 20:00 UTC). A
    naive ``ts > record["last_seen"]`` string compare would therefore keep
    the +05:00 stamp as "last_seen" after both are ingested, when the
    Z-suffixed one is the real later timestamp.
    """
    earlier_high_digit = "2026-09-09T23:00:00+05:00"   # 18:00 UTC
    later_low_digit = "2026-09-09T20:00:00Z"            # 20:00 UTC — really later
    assert earlier_high_digit > later_low_digit   # string order disagrees...
    assert (bl._parse_ts(earlier_high_digit)
            < bl._parse_ts(later_low_digit))      # ...with real time order

    ledger = bl.get_ledger()
    ledger.append([
        _entry(domain="a.example.com", ts=earlier_high_digit),
        _entry(domain="b.example.com", ts=later_low_digit),
    ])
    summary = ledger.summary(hours=24)
    device = summary["devices"][0]
    assert device["last_seen"] == later_low_digit


def test_device_summary_by_mac_key_and_ip():
    ledger = bl.get_ledger()
    _populate(ledger)
    assert ledger.device_summary("AA:BB:CC:00:00:01")["total_queries"] == 4
    assert ledger.device_summary("aa-bb-cc-00-00-01")["total_queries"] == 4
    assert ledger.device_summary("192.168.1.30")["total_queries"] == 1
    assert ledger.device_summary("ip:192.168.1.30")["total_queries"] == 1


# ── grouping resolves through the CURRENT inventory ─────────────────────

class _FakeInventory:
    """A box as the inventory has it: an ip-keyed row that knows its MAC."""

    KEY = "ip:192.168.50.40"
    MAC = "00:00:5E:00:53:41"

    def key_for_ip(self, ip):
        return self.KEY if ip == "192.168.50.40" else ""

    def mac_for_ip(self, ip):
        return self.MAC if ip == "192.168.50.40" else ""

    def get(self, wanted):
        if wanted in (self.KEY, self.MAC, "192.168.50.40"):
            return {"key": self.KEY, "mac": self.MAC, "ip": "192.168.50.40"}
        return None


@pytest.fixture
def orin_inventory(monkeypatch):
    import puffpai.bridge_inventory as bi
    monkeypatch.setattr(bi, "get_inventory", lambda: _FakeInventory())


def _orin_rows(ledger):
    """Two rows for one IP, stamped with the two keys the box really has.

    The ip-keyed row was ingested before the inventory had learned the MAC;
    the MAC-keyed one after. Append-only means both keys are on disk forever.
    """
    ledger.append([
        _entry(client="192.168.50.40", domain="samsungads.com",
               ts=_iso(minutes_ago=1), mac=""),
        _entry(client="192.168.50.40", domain="doubleclick.net",
               ts=_iso(minutes_ago=2), mac="00:00:5E:00:53:41"),
    ])


def test_summary_collapses_rows_the_inventory_now_maps_to_one_device(
        orin_inventory):
    """The box appearing twice, ip:192.168.50.40 and 00:00:5E:00:53:41."""
    ledger = bl.get_ledger()
    _orin_rows(ledger)
    summary = ledger.summary(hours=24)

    assert summary["entry_count"] == 2
    assert summary["device_count"] == 1
    device = summary["devices"][0]
    assert device["key"] == "ip:192.168.50.40"      # the inventory's own key
    assert device["mac"] == "00:00:5E:00:53:41"  # resolved, not the stored ""
    assert device["total_queries"] == 2


def test_device_summary_accepts_mac_key_or_ip_for_one_device(orin_inventory):
    ledger = bl.get_ledger()
    _orin_rows(ledger)
    for identifier in ("00:00:5E:00:53:41", "00-00-5e-00-53-41",
                       "ip:192.168.50.40", "192.168.50.40"):
        assert ledger.device_summary(identifier)["total_queries"] == 2, identifier


def test_summary_keeps_the_stored_key_when_the_inventory_is_cold(monkeypatch):
    """An IP the inventory has never seen must not lose its recorded MAC."""
    class _Empty:
        def key_for_ip(self, ip):
            return ""

        def mac_for_ip(self, ip):
            return ""

        def get(self, wanted):
            return None

    import puffpai.bridge_inventory as bi
    monkeypatch.setattr(bi, "get_inventory", lambda: _Empty())

    ledger = bl.get_ledger()
    _populate(ledger)
    summary = ledger.summary(hours=24)
    assert {d["key"] for d in summary["devices"]} == {
        "AA:BB:CC:00:00:01", "ip:192.168.1.30"}


def test_summary_survives_an_unavailable_inventory(monkeypatch):
    import puffpai.bridge_inventory as bi

    def boom():
        raise RuntimeError("inventory exploded")

    monkeypatch.setattr(bi, "get_inventory", boom)
    ledger = bl.get_ledger()
    _populate(ledger)
    assert ledger.summary(hours=24)["device_count"] == 2


def test_device_summary_of_an_unknown_device_is_zeroed():
    result = bl.get_ledger().device_summary("FF:FF:FF:00:00:00")
    assert result["total_queries"] == 0
    assert result["top_domains"] == []


# ── pull ────────────────────────────────────────────────────────────────────

def test_pull_is_a_noop_when_zone3_is_off(monkeypatch):
    monkeypatch.setattr(bl, "_adguard_settings", lambda: None)
    result = bl.get_ledger().pull()
    assert result == {"pulled": 0, "written": 0, "zone3": False,
                      "reason": "Zone 3 disabled"}
    assert not bl.telemetry_file().exists()


def test_pull_ingests_and_resolves_macs(monkeypatch, zone3_on):
    monkeypatch.setattr(bl, "fetch_querylog", lambda limit: [
        RAW,
        dict(RAW, client="192.168.1.30",
             question={"host": "puffpai.com"}, reason="NotFilteredNotFound"),
        {"garbage": True},
    ])

    class _Inventory:
        def mac_for_ip(self, ip):
            return "AA:BB:CC:00:00:01" if ip == "192.168.1.20" else ""

    import puffpai.bridge_inventory as bi
    monkeypatch.setattr(bi, "get_inventory", lambda: _Inventory())

    result = bl.get_ledger().pull()
    assert result["zone3"] is True
    assert result["pulled"] == 3          # raw rows seen
    assert result["written"] == 2         # the unusable row dropped

    entries = bl.get_ledger().read(hours=24 * 365)
    by_domain = {e["domain"]: e for e in entries}
    assert by_domain["samsungacr.com"]["mac"] == "AA:BB:CC:00:00:01"
    assert by_domain["puffpai.com"]["mac"] == ""


def test_pull_survives_an_unavailable_inventory(monkeypatch, zone3_on):
    monkeypatch.setattr(bl, "fetch_querylog", lambda limit: [RAW])

    import puffpai.bridge_inventory as bi

    def boom():
        raise RuntimeError("inventory exploded")

    monkeypatch.setattr(bi, "get_inventory", boom)
    assert bl.get_ledger().pull()["written"] == 1


def test_pull_dedup_survives_a_restart(monkeypatch, zone3_on):
    """A second BridgeLedger on the same data dir must not re-ingest.

    Simulates an api restart: AdGuard hands back the same rolling querylog
    window to a brand-new process, and the persisted high-water mark + seeded
    dedup keys must stop it from duplicating rows.
    """
    window = [
        dict(RAW, client="192.168.1.20", question={"host": "samsungacr.com"}),
        dict(RAW, client="192.168.1.30", question={"host": "puffpai.com"},
             reason="NotFilteredNotFound"),
    ]
    monkeypatch.setattr(bl, "fetch_querylog", lambda limit: window)

    first = bl.get_ledger()
    result1 = first.pull()
    assert result1["written"] == 2
    summary1 = first.summary(hours=24 * 365)

    # A brand-new instance on the same (tmp_path-pinned) data dir — as if the
    # api process restarted.
    bl.reset_ledger()
    second = bl.get_ledger()
    result2 = second.pull()
    assert result2["written"] == 0

    summary2 = second.summary(hours=24 * 365)
    assert summary2["entry_count"] == summary1["entry_count"]
    assert summary2["device_count"] == summary1["device_count"]


def test_future_row_does_not_poison_high_water(monkeypatch, zone3_on):
    """One clock-skewed AdGuard row must not silence every later pull.

    A row stamped a year in the future is ingested (recorded as-is) but must
    not become the persisted high-water mark — otherwise every subsequent
    pull's rows fall below the mark and are dropped as "already seen",
    forever, across restarts.
    """
    future_ts = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    future_row = dict(RAW, client="192.168.1.40",
                       question={"host": "future.example.com"},
                       time=future_ts, reason="NotFilteredNotFound")
    monkeypatch.setattr(bl, "fetch_querylog", lambda limit: [future_row])

    ledger = bl.get_ledger()
    result1 = ledger.pull()
    assert result1["written"] == 1

    normal_rows = [
        dict(RAW, client="192.168.1.20", question={"host": "samsungacr.com"},
             time=_iso()),
        dict(RAW, client="192.168.1.30", question={"host": "puffpai.com"},
             reason="NotFilteredNotFound", time=_iso()),
    ]
    monkeypatch.setattr(bl, "fetch_querylog", lambda limit: normal_rows)
    result2 = ledger.pull()
    assert result2["written"] == 2

    # A brand-new instance on the same data dir must see the same window as
    # already ingested (normal dedup), not re-poisoned by the future row.
    bl.reset_ledger()
    second = bl.get_ledger()
    monkeypatch.setattr(bl, "fetch_querylog", lambda limit: normal_rows)
    result3 = second.pull()
    assert result3["written"] == 0


def test_loaded_future_high_water_is_discarded(monkeypatch, zone3_on):
    """A state file carrying a future high-water mark must be discarded on load."""
    future_ts = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
    state_path = bl.ledger_state_file()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"last_ingested_ts": future_ts, "recent_keys": []}),
        encoding="utf-8")

    ledger = bl.BridgeLedger()
    assert ledger._high_water_ts == ""

    normal_rows = [
        dict(RAW, client="192.168.1.20", question={"host": "samsungacr.com"},
             time=_iso()),
        dict(RAW, client="192.168.1.30", question={"host": "puffpai.com"},
             reason="NotFilteredNotFound", time=_iso()),
    ]
    monkeypatch.setattr(bl, "fetch_querylog", lambda limit: normal_rows)
    result = ledger.pull()
    assert result["written"] > 0


def test_fetch_querylog_reads_the_raw_endpoint(monkeypatch, zone3_on):
    seen = {}

    def fake(base, user, pw, limit):
        seen.update(base=base, limit=limit)
        return [RAW]

    monkeypatch.setattr(bl, "_querylog_via_http", fake)
    assert bl.fetch_querylog(limit=7) == [RAW]
    assert seen == {"base": "http://192.168.50.140:3000", "limit": 7}


def test_normalize_accepts_the_recent_queries_projection():
    """AdGuardController.recent_queries drops `question` and `reason`.

    The ledger reads the raw endpoint instead (that projection caps at 500 rows
    and loses `upstream`), but normalize_row must still handle its shape so
    switching accessors later is a one-line change.
    """
    projected = {"time": "2026-09-09T10:00:00Z", "client": "192.168.1.20",
                 "domain": "samsungacr.com", "type": "A",
                 "reason": "", "rule": "", "blocked": True}
    entry = bl.normalize_row(projected, enforce=False)
    assert entry["domain"] == "samsungacr.com"
    assert entry["category"] == "TELEMETRY"
    assert entry["answer"] == "monitor"
    assert bl.normalize_row(projected, enforce=True)["answer"] == "blocked"
    unblocked = dict(projected, blocked=False)
    assert bl.normalize_row(unblocked, enforce=True)["answer"] == "allowed"


def test_fetch_querylog_is_empty_when_zone3_is_off(monkeypatch):
    monkeypatch.setattr(bl, "_adguard_settings", lambda: None)
    assert bl.fetch_querylog() == []


def test_querylog_via_http_degrades_on_error(monkeypatch):
    import requests
    from puffpai.security import lan_http

    def boom(*args, **kwargs):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(lan_http, "lan_get", boom)
    assert bl._querylog_via_http("http://x:3000", "", "", 10) == []


def test_querylog_via_http_reads_the_data_envelope(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [RAW]}

    def fake_get(url, params=None, auth=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _Resp()

    from puffpai.security import lan_http
    # Patched at puffpai.security.lan_http, not at ``requests``: the AdGuard
    # readers go through the house-local wrapper now, which resolves the
    # configured URL and refuses anything that is not loopback/RFC1918/ULA
    # before a socket opens. Patching ``requests`` here would patch nothing
    # and the test would dial the real address in the config.
    monkeypatch.setattr(lan_http, "lan_get", fake_get)
    rows = bl._querylog_via_http("http://192.168.50.140:3000", "", "", 50)
    assert rows == [RAW]
    assert captured["url"] == "http://192.168.50.140:3000/control/querylog"
    assert captured["params"] == {"limit": 50}


def test_status_reports_the_local_ledger(zone3_on):
    ledger = bl.get_ledger()
    ledger.append([_entry()])
    status = ledger.status()
    assert status["ledger_path"].endswith("bridge_telemetry.jsonl")
    assert status["ledger_bytes"] > 0
    assert status["zone3_enabled"] is True


def test_status_reports_last_written(monkeypatch, zone3_on):
    """A silenced ledger (e.g. a poisoned high-water mark) must be visible."""
    monkeypatch.setattr(bl, "fetch_querylog", lambda limit: [RAW])
    ledger = bl.get_ledger()
    result = ledger.pull()
    assert ledger.status()["last_written"] == result["written"] == 1


# ── file permissions ─────────────────────────────────────────────────────────

@pytest.mark.skipif(os.name != "posix", reason="POSIX file mode bits only")
def test_ledger_files_written_0600(monkeypatch, zone3_on):
    monkeypatch.setattr(bl, "fetch_querylog", lambda limit: [RAW])
    ledger = bl.get_ledger()
    ledger.pull()
    assert bl.telemetry_file().stat().st_mode & 0o777 == 0o600
    assert bl.ledger_state_file().stat().st_mode & 0o777 == 0o600


def test_ledger_files_write_succeeds_on_any_os(monkeypatch, zone3_on):
    """Windows-safe: the 0600 open flags must not break the writes themselves."""
    monkeypatch.setattr(bl, "fetch_querylog", lambda limit: [RAW])
    ledger = bl.get_ledger()
    result = ledger.pull()
    assert result["written"] == 1
    assert bl.telemetry_file().exists()
    assert bl.ledger_state_file().exists()
