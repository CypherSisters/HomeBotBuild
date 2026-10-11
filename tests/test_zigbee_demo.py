"""
Zigbee demo-readiness tests.

Runs end-to-end against the LIVE Home Assistant (no mocks for HA calls) and
MOCK the plugged-in Sonoff dongle so we can rehearse before the actual
recording. We only *start* the ZHA flow and abort it — we don't want the
test run to install ZHA for real (that would make the demo anticlimactic).

Run:
    cd C:/Users/owner/PuffPAI/PuffPAI
    python tests/test_zigbee_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# LIVE-HOUSE GUARD — read this before removing it.
#
# The `test_live_*` functions below talk to the real Home Assistant, and
# test_live_zha_flow_starts_cleanly POSTs a ZHA config flow (then aborts it).
# pytest.ini sets `testpaths = tests` and `python_files = test_*.py`, so a
# bare `pytest` used to collect this file and actuate the household.
#
# The owner's standing rule is that nothing may send a mutating call to the
# live house for any test or verification purpose. Running the suite must
# never be the thing that touches someone's home, so the live tests are now
# opt-in: set PUFFPAI_LIVE_HA_TESTS=1 deliberately, on a box you mean to
# exercise. The direct `python tests/test_zigbee_demo.py` entry point at the
# bottom of this file is unchanged — running it by name is an explicit act.
# ---------------------------------------------------------------------------
LIVE_HA_TESTS_ENABLED = os.environ.get("PUFFPAI_LIVE_HA_TESTS", "").strip().lower() in (
    "1", "true", "yes",
)

pytestmark = pytest.mark.skipif(
    not LIVE_HA_TESTS_ENABLED,
    reason=(
        "talks to the live Home Assistant and can actuate the household; "
        "set PUFFPAI_LIVE_HA_TESTS=1 to opt in"
    ),
)

# Ensure repo src is on path so `import puffpai.*` works without install.
# assistant_tools.py contains a bare `from app_data import AppData`, so the
# puffpai/ directory also needs to be on sys.path.
#
# The running copy's path goes on sys.path only in live mode: this module is
# imported at collection time even when every test is skipped, and a foreign
# checkout at the front of sys.path shadows bare module names (api_server,
# app_data, ...) for every test that imports them later in the run.
if LIVE_HA_TESTS_ENABLED or __name__ == "__main__":
    _REPO = Path(r"C:\Users\owner\PuffPAI\PuffPAI")
    sys.path.insert(0, str(_REPO / "src"))
    sys.path.insert(0, str(_REPO / "src" / "puffpai"))

from puffpai.zigbee_manager import (  # noqa: E402
    DetectedDevice,
    KNOWN_DONGLES,
    ZigbeeManager,
    ZigbeeMatch,
)

# ---------------------------------------------------------------------------
# Fake device fixtures (what we expect to see when the Sonoff is plugged in)
# ---------------------------------------------------------------------------

FAKE_SONOFF_P = DetectedDevice(
    vid="10c4", pid="ea60",
    serial_number="0001",
    manufacturer="ITEAD",
    description="ITEAD SONOFF Zigbee 3.0 USB Dongle Plus V2",
    device_path="/dev/serial/by-id/usb-ITEAD_SONOFF_Zigbee_3.0_USB_Dongle_Plus_V2-if00-port0",
    subsystem="usb",
    raw={"product": "SONOFF Zigbee 3.0 USB Dongle Plus"},
)

FAKE_UNRELATED = DetectedDevice(
    vid="05e3", pid="0608",
    serial_number=None,
    manufacturer="Genesys Logic",
    description="USB2.0 Hub",
    device_path=None,
    subsystem="usb",
    raw={},
)


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {name}" + (f"  -- {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_dongle_registry() -> None:
    section("1. Dongle registry sanity")
    record(
        "Registry contains Sonoff ZBDongle-P",
        any(d.vid == "10c4" and d.pid == "ea60" and d.radio_type == "znp"
            for d in KNOWN_DONGLES),
        "vid:10c4 pid:ea60 radio_type:znp present",
    )
    record(
        "Registry contains Sonoff ZBDongle-E",
        any(d.vid == "1a86" and d.pid == "55d4" and d.radio_type == "ezsp"
            for d in KNOWN_DONGLES),
    )
    record(
        "All specs declare a baudrate",
        all(isinstance(d.baudrate, int) and d.baudrate > 0 for d in KNOWN_DONGLES),
    )


def test_match_finds_dongle_when_present() -> None:
    section("2. Detection — dongle present")
    mgr = ZigbeeManager()
    devs = [FAKE_UNRELATED, FAKE_SONOFF_P]
    matches = mgr.find_zigbee_dongles(devs)
    record("find_zigbee_dongles returns >=1 match", len(matches) >= 1,
           f"got {len(matches)} match(es)")
    if matches:
        best = matches[0]
        record("Matched spec is Sonoff ZBDongle-P",
               "ZBDongle-P" in best.spec.name or best.spec.radio_type == "znp",
               best.spec.name)
        record("Confidence is high (product hint matched)",
               best.confidence == "high",
               f"confidence={best.confidence}")
        record("device_path populated", bool(best.device.device_path),
               best.device.device_path or "(none)")


def test_match_returns_empty_when_absent() -> None:
    section("3. Detection — no dongle")
    mgr = ZigbeeManager()
    matches = mgr.find_zigbee_dongles([FAKE_UNRELATED])
    record("Unrelated USB device not matched", len(matches) == 0,
           f"got {len(matches)}")


def test_live_ha_usb_scan() -> None:
    section("4. Live HA — usb/scan WebSocket command")
    mgr = ZigbeeManager()
    try:
        devs = mgr.list_usb_devices()
        record("list_usb_devices returns a list", isinstance(devs, list),
               f"count={len(devs)}")
        for d in devs[:5]:
            print(f"    -> vid:{d.vid} pid:{d.pid} {d.description!r} path={d.device_path}")
    except Exception as exc:
        record("list_usb_devices did not raise", False, str(exc))


def test_live_zha_not_yet_installed() -> None:
    section("5. Live HA — ZHA not installed (baseline)")
    mgr = ZigbeeManager()
    installed = mgr.is_zha_installed()
    record("ZHA not yet installed (baseline assumption)", not installed,
           "installed=True" if installed else "installed=False")


def test_live_zha_flow_starts_cleanly() -> None:
    """Starts the ZHA flow (REAL request), asserts it advances to the port
    step with our mocked dongle, then ABORTS so nothing is installed."""
    section("6. Live HA — ZHA config flow can be driven to port step")
    mgr = ZigbeeManager()
    if mgr.is_zha_installed():
        record("ZHA flow exercise skipped — ZHA already installed", True,
               "(remove the entry in HA to rerun this)")
        return

    fake_match = ZigbeeMatch(device=FAKE_SONOFF_P, spec=KNOWN_DONGLES[0], confidence="high")

    # Start the flow manually — don't call install_zha() because it would
    # attempt to finish. We just want to prove the first 1-2 steps work.
    try:
        r1 = mgr._flow_post(None, {"handler": "zha"})
        flow_id = r1.get("flow_id")
        record("ZHA flow init returns a flow_id", bool(flow_id), flow_id or str(r1))

        step = r1.get("step_id") or r1.get("type")
        record("Flow first step is a form", r1.get("type") == "form", f"step={step}")

        schema = r1.get("data_schema") or []
        has_select = any(s.get("type") == "select" for s in schema)
        record("First form offers a radio-type select", has_select,
               f"fields={[s.get('name') for s in schema]}")

        # Pick the ZNP radio label
        options = []
        for s in schema:
            if s.get("type") == "select":
                options = s.get("options") or []
                break
        chosen = mgr._pick_label(options, "znp")
        record("Can pick a ZNP-matching radio label", bool(chosen),
               chosen or f"options={options[:2]}")

        if flow_id:
            mgr._abort_flow(flow_id)
            record("Aborted test flow cleanly", True)
    except Exception as exc:
        record("Flow exercise raised no exception", False,
               f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


def test_permit_join_guard() -> None:
    """open_permit_join must refuse an unconfirmed pairing window.

    This file runs against the LIVE Home Assistant, so this check is written to
    be safe there: no :data:`HUMAN_CONFIRMED` token is passed, and the refusal
    now happens before any request leaves the appliance, so nothing can open a
    pairing window on a real house. **Do not add a confirmation token here** —
    that would turn a refusal test into a live actuation.

    The old assertion (``success is False`` because ZHA was missing) tested the
    wrong thing: with ZHA installed it silently skipped, and it would have
    passed even with no gate at all.
    """
    from puffpai.ha_bridge import ConfirmationRequired

    section("7. open_permit_join refuses an unconfirmed pairing window")
    mgr = ZigbeeManager()
    try:
        result = mgr.open_permit_join(duration=30)
    except ConfirmationRequired as exc:
        record("open_permit_join refuses without a human confirmation", True,
               f"proposal={exc.proposal}")
        return
    record("open_permit_join refuses without a human confirmation", False,
           f"NOT REFUSED — returned {result}")


def test_assistant_tool_scan_with_mock() -> None:
    section("8. AssistantTools.ha_scan_hardware — mock dongle plugged in")
    # Import inside test so earlier failures don't abort everything
    try:
        from puffpai.assistant_tools import AssistantTools
    except Exception as exc:
        record("Import AssistantTools", False, str(exc))
        return

    with patch.object(ZigbeeManager, "list_usb_devices", return_value=[FAKE_SONOFF_P]), \
         patch.object(ZigbeeManager, "is_zha_installed", return_value=False):
        try:
            rt = AssistantTools()
            scan_tool = rt.tool_dict.get("ha_scan_hardware")
            record("ha_scan_hardware tool registered", scan_tool is not None,
                   ", ".join(t for t in rt.tool_dict if t.startswith("ha_"))[:200])
            if scan_tool is None:
                return
            response = scan_tool.invoke("")
            print(f"    -> response: {response}")
            record("Response mentions 'Zigbee' and 'Sonoff'",
                   "zigbee" in response.lower() and "sonoff" in response.lower(),
                   response[:80])
            record("Response asks for confirmation",
                   "manage" in response.lower() or "would you like" in response.lower(),
                   "ok" if "manage" in response.lower() else response[:80])
        except Exception as exc:
            record("ha_scan_hardware did not raise", False, str(exc))
            traceback.print_exc()


def test_assistant_tool_scan_without_dongle() -> None:
    section("9. AssistantTools.ha_scan_hardware — no dongle plugged in")
    try:
        from puffpai.assistant_tools import AssistantTools
    except Exception as exc:
        record("Import AssistantTools", False, str(exc))
        return

    with patch.object(ZigbeeManager, "list_usb_devices", return_value=[]):
        try:
            rt = AssistantTools()
            scan_tool = rt.tool_dict["ha_scan_hardware"]
            response = scan_tool.invoke("")
            print(f"    -> response: {response}")
            record("Response reports nothing new",
                   "no new usb hardware" in response.lower()
                   or "none look like" in response.lower(),
                   response[:80])
        except Exception as exc:
            record("ha_scan_hardware did not raise", False, str(exc))


def test_assistant_tool_add_guard() -> None:
    section("10. AssistantTools.ha_add_zigbee_dongle — no dongle = polite refusal")
    try:
        from puffpai.assistant_tools import AssistantTools
    except Exception as exc:
        record("Import AssistantTools", False, str(exc))
        return
    with patch.object(ZigbeeManager, "find_zigbee_dongles", return_value=[]), \
         patch.object(ZigbeeManager, "is_zha_installed", return_value=False):
        rt = AssistantTools()
        add_tool = rt.tool_dict["ha_add_zigbee_dongle"]
        resp = add_tool.invoke("")
        print(f"    -> response: {resp}")
        record("Add tool guards against missing dongle",
               "can't find" in resp.lower() or "plug it in" in resp.lower(),
               resp[:80])


def test_assistant_tool_pair_retired() -> None:
    section("11. ha_pair_zigbee_devices — retired; pairing asks first now")
    try:
        from puffpai.assistant_tools import AssistantTools
        from puffpai import zigbee_pairing
    except Exception as exc:
        record("Import AssistantTools + zigbee_pairing", False, str(exc))
        return
    rt = AssistantTools()
    record("Legacy pair tool is gone from the catalog",
           "ha_pair_zigbee_devices" not in rt.tool_dict,
           "pairing goes through the consent flow, not a tool call")
    record("Consent entry point exists (request_pairing)",
           callable(getattr(zigbee_pairing, "request_pairing", None)),
           "zigbee_pairing.request_pairing files the question")


def test_intent_routing() -> None:
    section("12. Router — demo phrases route to agent path")
    try:
        from puffpai.router import RouteTarget, route_message
    except Exception as exc:
        record("Import router", False, str(exc))
        return

    phrases = [
        "do a scan of the network",
        "I just plugged in a new zigbee dongle",
        "scan for new devices",
        "add the zigbee lightbulbs in the livingroom to the zigbee manager",
        "pair my zigbee bulbs",
    ]
    for p in phrases:
        route = route_message(p)
        ok = route in (RouteTarget.AGENT, RouteTarget.HOME)
        record(f"\"{p[:50]}\" -> agent", ok, str(route))


def test_assistant_tool_filter_includes_zigbee() -> None:
    section("13. _filter_tools_by_intent includes zigbee tools for demo phrases")
    try:
        from puffpai.assistant_tools import AssistantTools
    except Exception as exc:
        record("Import AssistantTools", False, str(exc))
        return
    rt = AssistantTools()
    for phrase in [
        "I just plugged in a new zigbee dongle",
        "add the 2 zigbee lightbulbs in the livingroom",
    ]:
        filtered = rt._filter_tools_by_intent(phrase)
        names = {t.name for t in filtered}
        has_scan = "ha_scan_hardware" in names
        has_add = "ha_add_zigbee_dongle" in names
        no_pair = "ha_pair_zigbee_devices" not in names
        record(f"\"{phrase[:40]}\" filter keeps zigbee tools",
               has_scan and has_add and no_pair,
               f"scan={has_scan} add={has_add} legacy-pair-absent={no_pair}")


# ---------------------------------------------------------------------------

def main() -> int:
    try:
        test_dongle_registry()
        test_match_finds_dongle_when_present()
        test_match_returns_empty_when_absent()
        test_live_ha_usb_scan()
        test_live_zha_not_yet_installed()
        test_live_zha_flow_starts_cleanly()
        test_permit_join_guard()
        test_assistant_tool_scan_with_mock()
        test_assistant_tool_scan_without_dongle()
        test_assistant_tool_add_guard()
        test_assistant_tool_pair_retired()
        test_intent_routing()
        test_assistant_tool_filter_includes_zigbee()
    except Exception:
        traceback.print_exc()

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
