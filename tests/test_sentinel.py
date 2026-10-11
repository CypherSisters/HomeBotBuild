"""
Device Sentinel tests — first-seed, new-device alerts, auto-quarantine.

Run:
    python tests/test_sentinel.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch

import pytest

# This checkout's src — a path from another checkout here would shadow bare
# module names (api_server, app_data, ...) for every later-importing test.
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "src" / "puffpai"))

# Import with a temp data dir so we don't clobber any real baseline
import puffpai.agent_events as agent_events  # noqa: E402
import puffpai.device_sentinel as ds  # noqa: E402


@pytest.fixture(autouse=True)
def _agent_event_log_in_tmp(monkeypatch, tmp_path):
    """scan_once() routes each new-device alert through
    puffpai.agent_events.submit_agent_event, which appends to the module's
    cached EVENT_LOG_PATH — the real data dir, since _fresh_sentinel only
    repoints device_sentinel's own files. Pin the cache to tmp (and drop the
    in-process line count that goes with it) so runs leave no residue."""
    monkeypatch.setattr(agent_events, "EVENT_LOG_PATH",
                        str(tmp_path / "agent_events.jsonl"))
    monkeypatch.setattr(agent_events, "_line_count", None)

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


# ---------------------------------------------------------------------------
# Sample fingerprints for deterministic tests
# ---------------------------------------------------------------------------

FP_IP_KNOWN = ds.Fingerprint(source="ip", key="AA:BB:CC:00:00:01",
                             label="192.168.1.10", extra={"ip": "192.168.1.10"})
FP_IP_ROGUE = ds.Fingerprint(source="ip", key="FF:EE:DD:00:00:99",
                             label="192.168.1.99", extra={"ip": "192.168.1.99"})
FP_USB_KNOWN = ds.Fingerprint(source="usb", key="05e3:0608:hub-1",
                              label="USB Hub")
FP_HA_NEW = ds.Fingerprint(source="ha", key="light.new_bulb",
                           label="New Bulb",
                           extra={"entity_id": "light.new_bulb"})


def _fresh_sentinel(tmp: Path) -> ds.DeviceSentinel:
    """Sentinel with a temp data dir so tests don't write to the real one."""
    # Monkey-patch module-level paths to point at tmp
    ds._DATA_DIR = tmp
    ds._BASELINE_FILE = tmp / "sentinel_baseline.json"
    ds._ALERTS_FILE = tmp / "sentinel_alerts.jsonl"
    # Drop the singleton so get_sentinel() would make a new one, but we
    # prefer to build it directly to control constructor args cleanly.
    ds._instance = None
    return ds.DeviceSentinel(interval_seconds=999)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_first_scan_seeds_no_alerts() -> None:
    section("1. First-ever scan seeds baseline, emits no alerts")
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        s = _fresh_sentinel(tmp)
        with patch.object(s, "_scan_ip", return_value=[FP_IP_KNOWN]), \
             patch.object(s, "_scan_usb", return_value=[FP_USB_KNOWN]), \
             patch.object(s, "_scan_ha", return_value=[]):
            result = s.scan_once()
        record("seeded=True after first scan", result.get("seeded") is True)
        record("baseline_size includes all sources", result.get("baseline_size") == 2,
               f"baseline={result.get('baseline_size')}")
        record("new_devices=0 on seed", result.get("new_devices") == 0)
        record("no alerts generated", len(s.list_alerts()) == 0)


def test_second_scan_alerts_on_new() -> None:
    section("2. After baseline, new device produces alert + quarantine")
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        s = _fresh_sentinel(tmp)
        # Seed
        with patch.object(s, "_scan_ip", return_value=[FP_IP_KNOWN]), \
             patch.object(s, "_scan_usb", return_value=[]), \
             patch.object(s, "_scan_ha", return_value=[]):
            s.scan_once()

        # Stub the firewall so we don't actually block anything.
        class _FakeFW:
            def block_device(self, ip, reason=""):
                return {"success": True, "message": f"fake-block {ip}"}

        with patch("puffpai.device_sentinel.DeviceSentinel._scan_ip",
                   return_value=[FP_IP_KNOWN, FP_IP_ROGUE]), \
             patch.object(s, "_scan_usb", return_value=[]), \
             patch.object(s, "_scan_ha", return_value=[]), \
             patch("puffpai.firewall_manager.get_firewall_backend", return_value=_FakeFW()):
            result = s.scan_once()

        record("new_devices=1 after rogue appears", result.get("new_devices") == 1,
               f"new_devices={result.get('new_devices')}")
        alerts = s.list_alerts()
        record("one unacknowledged alert", len(alerts) == 1, f"count={len(alerts)}")
        if alerts:
            a = alerts[0]
            record("alert source=ip", a["source"] == "ip")
            record("alert is quarantined", a["quarantined"] is True,
                   a["quarantine_detail"])
            record("alert summary mentions 'blocked'",
                   "blocked" in a["summary"].lower(), a["summary"][:80])


def test_usb_alert_not_quarantined() -> None:
    section("3. New USB device alerts but is not auto-disabled")
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        s = _fresh_sentinel(tmp)
        # Seed empty
        with patch.object(s, "_scan_ip", return_value=[]), \
             patch.object(s, "_scan_usb", return_value=[]), \
             patch.object(s, "_scan_ha", return_value=[]):
            s.scan_once()
        # New USB appears
        new_usb = ds.Fingerprint(source="usb", key="10c4:ea60:abc",
                                 label="Sonoff ZBDongle-P")
        with patch.object(s, "_scan_ip", return_value=[]), \
             patch.object(s, "_scan_usb", return_value=[new_usb]), \
             patch.object(s, "_scan_ha", return_value=[]):
            s.scan_once()

        alerts = s.list_alerts()
        record("USB alert exists", len(alerts) == 1)
        if alerts:
            a = alerts[0]
            record("USB alert NOT quarantined", a["quarantined"] is False)
            record("USB alert summary warns user",
                   "unplug" in a["summary"].lower(), a["summary"][:80])


def test_callback_fires() -> None:
    section("4. on_alert callback is invoked for each new device")
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        ds._DATA_DIR = tmp
        ds._BASELINE_FILE = tmp / "b.json"
        ds._ALERTS_FILE = tmp / "a.jsonl"
        ds._instance = None
        received = []
        s = ds.DeviceSentinel(interval_seconds=999,
                              on_alert=lambda a: received.append(a))
        # Seed
        with patch.object(s, "_scan_ip", return_value=[]), \
             patch.object(s, "_scan_usb", return_value=[]), \
             patch.object(s, "_scan_ha", return_value=[]):
            s.scan_once()
        # Trigger alert
        with patch.object(s, "_scan_ip", return_value=[]), \
             patch.object(s, "_scan_usb", return_value=[FP_USB_KNOWN]), \
             patch.object(s, "_scan_ha", return_value=[]):
            s.scan_once()
        record("callback received exactly 1 alert", len(received) == 1,
               f"got {len(received)}")


def test_baseline_persistence() -> None:
    section("5. Baseline persists across sentinel restarts")
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        s = _fresh_sentinel(tmp)
        with patch.object(s, "_scan_ip", return_value=[FP_IP_KNOWN]), \
             patch.object(s, "_scan_usb", return_value=[]), \
             patch.object(s, "_scan_ha", return_value=[]):
            s.scan_once()
        size1 = s.status()["baseline_size"]

        # New instance reading from the same files
        s2 = _fresh_sentinel(tmp)
        size2 = s2.status()["baseline_size"]
        record("baseline survives reload", size1 == size2 and size1 > 0,
               f"before={size1} after={size2}")
        record("seeded flag survives reload", s2._seeded is True)

        # No alert on second run for the same device
        with patch.object(s2, "_scan_ip", return_value=[FP_IP_KNOWN]), \
             patch.object(s2, "_scan_usb", return_value=[]), \
             patch.object(s2, "_scan_ha", return_value=[]):
            r = s2.scan_once()
        record("known device not re-alerted after reload",
               r.get("new_devices") == 0)


def test_acknowledge_and_approve() -> None:
    section("6. Alert acknowledge + approve flow")
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        s = _fresh_sentinel(tmp)
        # Seed
        with patch.object(s, "_scan_ip", return_value=[]), \
             patch.object(s, "_scan_usb", return_value=[]), \
             patch.object(s, "_scan_ha", return_value=[]):
            s.scan_once()
        class _FW:
            calls = []
            def block_device(self, ip, reason=""):
                self.calls.append(("block", ip))
                return {"success": True}
            def unblock_device(self, ip):
                self.calls.append(("unblock", ip))
                return {"success": True, "message": "ok"}
        fw = _FW()
        with patch.object(s, "_scan_ip", return_value=[FP_IP_ROGUE]), \
             patch.object(s, "_scan_usb", return_value=[]), \
             patch.object(s, "_scan_ha", return_value=[]), \
             patch("puffpai.firewall_manager.get_firewall_backend", return_value=fw):
            s.scan_once()
        alerts = s.list_alerts()
        record("exactly one open alert", len(alerts) == 1)
        aid = alerts[0]["id"]
        ack = s.acknowledge(aid)
        record("acknowledge succeeds", ack.get("success") is True)
        record("no open alerts after ack", len(s.list_alerts()) == 0)

        # Approve: should call unblock via fake FW.  We need ARP to resolve
        # the MAC back to the IP — mock _scan_ip so _ip_for_mac finds it.
        with patch.object(s, "_scan_ip", return_value=[FP_IP_ROGUE]), \
             patch("puffpai.firewall_manager.get_firewall_backend", return_value=fw):
            result = s.approve_device(aid)
        record("approve_device returns success", result.get("success") is True,
               result.get("detail", ""))
        unblock_called = any(c[0] == "unblock" for c in fw.calls)
        record("approve triggers firewall unblock", unblock_called,
               f"fw calls: {fw.calls}")


def test_status_shape() -> None:
    section("7. status() shape is UI-ready")
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        s = _fresh_sentinel(tmp)
        status = s.status()
        expected_keys = {"running", "interval_seconds", "baseline_size",
                         "seeded", "last_scan_at", "last_scan_counts",
                         "pending_alerts", "total_alerts", "recent_errors"}
        missing = expected_keys - set(status)
        record("status() returns all expected keys", not missing,
               f"missing={missing}")


def test_alerts_log_is_jsonl() -> None:
    section("8. sentinel_alerts.jsonl is well-formed JSONL")
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        s = _fresh_sentinel(tmp)
        with patch.object(s, "_scan_ip", return_value=[]), \
             patch.object(s, "_scan_usb", return_value=[]), \
             patch.object(s, "_scan_ha", return_value=[]):
            s.scan_once()
        with patch.object(s, "_scan_ip", return_value=[]), \
             patch.object(s, "_scan_usb", return_value=[FP_USB_KNOWN]), \
             patch.object(s, "_scan_ha", return_value=[]):
            s.scan_once()

        log = ds._ALERTS_FILE
        record("alerts log file exists", log.exists())
        if log.exists():
            lines = [l for l in log.read_text().splitlines() if l.strip()]
            record("alerts log has >=1 line", len(lines) >= 1, f"lines={len(lines)}")
            ok = True
            for l in lines:
                try:
                    json.loads(l)
                except Exception:
                    ok = False
            record("every line is valid JSON", ok)


# ---------------------------------------------------------------------------
# Differential: the sentinel's resolver vs. the canonical one
# ---------------------------------------------------------------------------
# _resolve_data_dir must not be a hand-rolled copy of config.resolve_state_dir's
# $PUFFPAI_DATA_DIR handling, because device_sentinel stays import-light and
# importing puffpai.config reaches the secrets provider. It delegates to
# puffpai.statedir instead -- a stdlib-only leaf that keeps the module just as
# import-light and defers that config import to call time. A hand-rolled copy
# would be free to drift, and a per-module copy that drifts (no strip / no
# expanduser / no repo-root join) is what would put audit trails outside the
# writable root.
#
# These checks are kept as a differential against the canon rather than
# retired with the copy: they are what would catch the leaf being swapped for
# something that answers differently. tests/test_statedir_call_sites.py covers
# the same contract for the other migrated modules.
#
# Note _resolve_data_dir() reads the env at call time and holds no cache, so no
# reload is needed. The module's _DATA_DIR is now lazy too (PEP 562), but these
# test the function, never the cached attribute. Pytest-only (fixtures); main()
# below is the standalone-script path and does not run them.


class TestResolveDataDirMatchesCanonicalResolver:
    @staticmethod
    def _expected() -> Path:
        from puffpai.config import resolve_state_dir
        return Path(resolve_state_dir(
            None,
            repo_root=str(ds._REPO_ROOT),
            default=str(ds._REPO_ROOT / "data"),
        ))

    def test_unset_falls_back_to_the_repo_data_dir(self, monkeypatch) -> None:
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        assert ds._resolve_data_dir() == self._expected() == ds._REPO_ROOT / "data"

    def test_empty_value_is_ignored(self, monkeypatch) -> None:
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "")
        assert ds._resolve_data_dir() == self._expected()

    def test_whitespace_only_value_is_ignored(self, monkeypatch) -> None:
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "   ")
        assert ds._resolve_data_dir() == self._expected()

    def test_absolute_value_is_honoured(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        assert ds._resolve_data_dir() == self._expected() == tmp_path / "state"

    def test_relative_value_joins_the_repo_root_not_the_cwd(self, monkeypatch) -> None:
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "relstate")
        assert ds._resolve_data_dir() == self._expected() == ds._REPO_ROOT / "relstate"

    def test_tilde_value_is_expanded(self, monkeypatch) -> None:
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "~/puff_state")
        resolved = ds._resolve_data_dir()
        assert resolved == self._expected()
        assert "~" not in str(resolved)


# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_first_scan_seeds_no_alerts,
        test_second_scan_alerts_on_new,
        test_usb_alert_not_quarantined,
        test_callback_fires,
        test_baseline_persistence,
        test_acknowledge_and_approve,
        test_status_shape,
        test_alerts_log_is_jsonl,
    ]
    for t in tests:
        try:
            t()
        except Exception:
            traceback.print_exc()
            record(t.__name__, False, "raised")

    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    for name, ok, detail in RESULTS:
        print(f"  {'[OK]' if ok else '[X] '} {name}")
    print(f"\n  {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
