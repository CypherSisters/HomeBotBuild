"""The registry must not declare ``security.zone3`` live while the box has
no Zone 3 at all.

The failure mode:

    GET /zone3/status
    -> {"success":false,
        "message":"Zone 3 not enabled. Set zone3.enabled=true in config and
                   start Pi-hole."}

...while the capability registry answers "live" for ``security.zone3``, so
"what can you do" advertises a network edge that does not exist.

Scope: this is the Pi-hole/AdGuard network-edge DNS feature ONLY. The rest of
the Bridge zone, the appliance's own default-deny firewall and its enumerated
doors, is live and must keep being described that way. The assertions below check both
halves: Zone 3 stops being claimed, and the firewall narrative survives.
"""

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from puffpai import capability_probes, capability_registry


@pytest.fixture(autouse=True)
def _clean_probe_cache():
    capability_probes.reset_cache()
    yield
    capability_probes.reset_cache()


def _config(zone3_enabled: bool):
    return SimpleNamespace(zone3=SimpleNamespace(enabled=zone3_enabled))


class TestZone3AvailabilityFollowsStatus:
    def test_disabled_zone3_is_not_advertised(self):
        with patch("puffpai.config.get_config", return_value=_config(False)):
            available, reason = capability_probes.probe("security.zone3")
        assert available is False
        assert "Zone 3" in reason

    def test_disabled_zone3_reports_offline_not_live(self):
        cap = capability_registry.get("security.zone3")
        assert cap.status == capability_registry.LIVE  # still declared live
        with patch("puffpai.config.get_config", return_value=_config(False)):
            assert capability_registry.runtime_status(cap) == capability_registry.OFFLINE

    def test_enabled_zone3_is_live_again_without_a_code_change(self):
        """The point of probing: starting Pi-hole must be enough."""
        cap = capability_registry.get("security.zone3")
        with patch("puffpai.config.get_config", return_value=_config(True)):
            assert capability_registry.runtime_status(cap) == capability_registry.LIVE
            assert capability_registry.runtime_availability(cap)[0] is True

    def test_serialized_entry_carries_the_runtime_answer(self):
        with patch("puffpai.config.get_config", return_value=_config(False)):
            entry = next(e for e in capability_registry.serialized_by_agent()["sentinel"]
                         if e["id"] == "security.zone3")
        assert entry["status"] == capability_registry.OFFLINE
        assert entry["available"] is False
        assert entry["unavailable_reason"]

    def test_spoken_security_area_no_longer_claims_a_zone_3_edge(self):
        with patch("puffpai.config.get_config", return_value=_config(False)):
            text = capability_registry.describe("security", for_voice=True)
        line = next(ln for ln in text.splitlines() if "Zone-3" in ln)
        assert "(currently offline)" in line

    def test_the_bridge_narrative_survives(self):
        """The box's own firewall and its doors are real and must not be
        described as gone along with Zone 3."""
        with patch("puffpai.config.get_config", return_value=_config(False)):
            _available, reason = capability_probes.probe("security.zone3")
        assert "firewall" in reason.lower()
        assert "inbound" in reason.lower()

    def test_probe_failure_fails_closed(self):
        with patch("puffpai.config.get_config", side_effect=RuntimeError("boom")):
            available, reason = capability_probes.probe("security.zone3")
        assert available is False
        # The exception TYPE may be named; its message must not be.
        assert "RuntimeError" in reason
        assert "boom" not in reason

    def test_unprobed_capabilities_keep_their_declaration(self):
        cap = capability_registry.get("weather.alerts")
        assert capability_probes.probe(cap.id) == (True, "")
        assert capability_registry.runtime_status(cap) == cap.status

    def test_probe_answers_are_cached_then_refreshable(self):
        calls = []

        def _counting():
            calls.append(1)
            return _config(False)

        with patch("puffpai.config.get_config", side_effect=_counting):
            capability_probes.probe("security.zone3")
            capability_probes.probe("security.zone3")
            assert len(calls) == 1
            capability_probes.reset_cache()
            capability_probes.probe("security.zone3")
            assert len(calls) == 2


class TestRegistryStillValidates:
    def test_no_capability_lost_its_entry(self):
        ids = {c.id for c in capability_registry.all_capabilities()}
        assert "security.zone3" in ids
        assert "security.network" in ids

    def test_zone3_routing_is_unchanged(self):
        """Probing changes the WORDS, never where a phrase routes."""
        with patch("puffpai.config.get_config", return_value=_config(False)):
            cap = capability_registry.find("what's my zone 3 status")
        assert cap is not None and cap.id == "security.zone3"
