"""Zone 3 bucket policy — the Home Assistant integration half of the firewall.

Four properties, in the order they matter:

1. **The subject is the HUB, not the gadget.** A cloud-backed integration means
   Home Assistant holds the vendor credential and Home Assistant is the client
   that talks to the vendor's cloud. A rule scoped to a smart plug's own IP
   refuses nothing at all, so every rule this produces carries
   ``$client=<hub address>`` — and when the hub's address is not a usable rule
   target, the engine REFUSES rather than falling back to a house-wide block.

2. **LOCKDOWN breaks the integration, and says so.** It is the one Zone 3 act
   whose whole point is that something stops working. The warning is generated
   in one place (:func:`bucket_lockdown_warning`) so the engine result, the 409
   proposal and the enforce preview cannot describe it in three strengths.

3. **A local integration is refused.** Nothing leaves the house for it, so a
   rule would create the appearance of protection and nothing else.

4. **Provenance survives.** ``recorded_by`` is on the row and in the preview,
   because the enforce switch turns every recorded rule live at once and a row
   a chat tool wrote is not a row the owner approved.

Nothing here touches a network. ``bridge_ha`` is stubbed, the AdGuard
controller is a recorder, and every address is fabricated.

Run: pytest tests/test_zone3_buckets.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffpai.zone3.policy_engine import (      # noqa: E402
    BUCKET_MODE_EFFECTS,
    BucketPolicy,
    PolicyEngine,
    SecurityMode,
    bucket_lockdown_warning,
)

# Fabricated. None of these exist on any network.
HUB = "192.168.50.140"
RING = "cloud:ring"
HUE = "local:hue"
ESP = "wifi_direct:esphome"

BUCKET_ROLL = {
    RING: {"id": RING, "tier": "cloud", "integration": "ring",
           "device_count": 3, "lookups": 412, "watching": 121},
    HUE: {"id": HUE, "tier": "local", "integration": "hue",
          "device_count": 9, "lookups": 0, "watching": 0},
    ESP: {"id": ESP, "tier": "wifi_direct", "integration": "esphome",
          "device_count": 2, "lookups": 4, "watching": 0},
}


class RecordingAdGuard:
    """An AdGuard that records what it was asked to write and writes nothing."""

    def __init__(self, enforce=False, reachable=True):
        self.enforce = enforce
        self._reachable = reachable
        self.blocks: list[list[str]] = []
        self.clients_upserted: list[tuple] = []

    def is_reachable(self):
        return self._reachable

    def clients(self):
        return []

    def delete_client(self, name):
        return True

    def upsert_client(self, name, ids, *, filtering_enabled=True):
        self.clients_upserted.append((name, tuple(ids), filtering_enabled))
        return True

    def replace_rule_block(self, body, *, note=""):
        self.blocks.append(list(body))
        return True

    @property
    def last_block(self):
        return self.blocks[-1] if self.blocks else []


@pytest.fixture
def hub(monkeypatch):
    """Point bridge_ha at a fabricated roll and a fabricated hub address."""
    from puffpai import bridge_ha
    monkeypatch.setattr(bridge_ha, "ha_host", lambda: HUB)
    monkeypatch.setattr(bridge_ha, "bucket",
                        lambda bid, *a, **k: BUCKET_ROLL.get(bid))
    monkeypatch.setattr(bridge_ha, "buckets",
                        lambda *a, **k: list(BUCKET_ROLL.values()))
    return HUB


def _engine(tmp_path, *, enforce=False, adguard=None):
    managed = tmp_path / "devices.json"
    managed.write_text("[]")
    return PolicyEngine(
        adguard=adguard if adguard is not None else RecordingAdGuard(enforce),
        managed_file=str(managed))


# ══════════════════════════════════════════════════════════════════════
#  1. The rules name the hub
# ══════════════════════════════════════════════════════════════════════

class TestRulesAreScopedToTheHub:

    def test_every_rule_carries_the_hub_as_its_client(self, tmp_path, hub):
        engine = _engine(tmp_path)
        result = engine.apply_bucket_policy(RING, SecurityMode.LOCKDOWN,
                                            recorded_by="owner")
        assert result["success"] is True
        assert result["client_ip"] == HUB
        rules = [ln for ln in engine.build_bucket_block()
                 if not ln.startswith("!")]
        assert rules, "LOCKDOWN on a known vendor produced no rules"
        for rule in rules:
            assert rule.startswith("||"), rule
            assert rule.endswith(f"^$client={HUB}"), rule

    def test_lockdown_includes_the_vendor_cloud_api(self, tmp_path, hub):
        """The difference between LOCKDOWN and BALANCED, stated as a rule set."""
        engine = _engine(tmp_path)
        engine.apply_bucket_policy(RING, SecurityMode.LOCKDOWN)
        locked = set(engine.get_bucket_policy(RING)["blocked_domains"])
        engine.apply_bucket_policy(RING, SecurityMode.BALANCED)
        balanced = set(engine.get_bucket_policy(RING)["blocked_domains"])
        assert "ring.com" in locked
        assert "ring.com" not in balanced, (
            "BALANCED must leave the vendor's cloud API resolvable, or it is "
            "LOCKDOWN wearing a friendlier name")

    def test_always_allow_writes_no_rule_at_all(self, tmp_path, hub):
        engine = _engine(tmp_path)
        engine.apply_bucket_policy(RING, SecurityMode.ALWAYS_ALLOW)
        assert engine.get_bucket_policy(RING)["blocked_domains"] == []
        assert [ln for ln in engine.build_bucket_block()
                if not ln.startswith("!")] == []

    def test_no_usable_hub_address_is_a_refusal_not_a_housewide_rule(
            self, tmp_path, monkeypatch, hub):
        """The failure mode that matters: an unscoped rule is not a smaller
        version of this policy, it is a house-wide block of a vendor cloud."""
        from puffpai import bridge_ha
        monkeypatch.setattr(bridge_ha, "ha_host", lambda: "")
        engine = _engine(tmp_path)
        result = engine.apply_bucket_policy(RING, SecurityMode.BALANCED)
        assert result["success"] is False
        assert "house-wide" in result["message"]
        assert engine.get_managed_buckets() == []

    def test_a_hub_address_that_is_not_an_ip_is_refused(self, tmp_path,
                                                        monkeypatch, hub):
        from puffpai import bridge_ha
        monkeypatch.setattr(bridge_ha, "ha_host", lambda: "homeassistant.local")
        engine = _engine(tmp_path)
        assert engine.apply_bucket_policy(RING,
                                          SecurityMode.BALANCED)["success"] \
            is False


# ══════════════════════════════════════════════════════════════════════
#  2. LOCKDOWN says what it breaks
# ══════════════════════════════════════════════════════════════════════

class TestLockdownWarnsPlainly:

    def test_the_result_carries_the_breaks_warning(self, tmp_path, hub):
        engine = _engine(tmp_path)
        result = engine.apply_bucket_policy(RING, SecurityMode.LOCKDOWN)
        assert result["breaks_integration"] is True
        assert "BREAKS the ring integration" in result["warning"]
        assert "3 device(s)" in result["warning"]
        assert result["warning"] in result["message"]

    def test_balanced_does_not_claim_to_break_anything(self, tmp_path, hub):
        engine = _engine(tmp_path)
        result = engine.apply_bucket_policy(RING, SecurityMode.BALANCED)
        assert result["breaks_integration"] is False
        assert "warning" not in result
        assert "keeps working" in BUCKET_MODE_EFFECTS["balanced"]

    def test_the_warning_has_one_source(self):
        """One function, so three surfaces cannot phrase it three ways."""
        assert bucket_lockdown_warning("ring", 3) == \
            bucket_lockdown_warning("ring", 3)
        assert "BREAKS" in bucket_lockdown_warning("ring")

    def test_the_rule_block_labels_the_breaking_bucket(self, tmp_path, hub):
        engine = _engine(tmp_path)
        engine.apply_bucket_policy(RING, SecurityMode.LOCKDOWN)
        block = "\n".join(engine.build_bucket_block())
        assert "BREAKS THIS INTEGRATION" in block


# ══════════════════════════════════════════════════════════════════════
#  3. Refusals
# ══════════════════════════════════════════════════════════════════════

class TestRefusals:

    def test_a_local_bucket_is_refused_with_a_reason(self, tmp_path, hub):
        engine = _engine(tmp_path)
        result = engine.apply_bucket_policy(HUE, SecurityMode.LOCKDOWN)
        assert result["success"] is False
        assert "local only" in result["message"]
        assert "nothing to block" in result["message"]
        assert engine.get_managed_buckets() == []

    def test_a_wifi_direct_bucket_is_refused(self, tmp_path, hub):
        """Not cloud-backed: PuffPAI will not guess what its traffic is."""
        engine = _engine(tmp_path)
        result = engine.apply_bucket_policy(ESP, SecurityMode.BALANCED)
        assert result["success"] is False
        assert engine.get_managed_buckets() == []

    def test_an_unknown_bucket_is_refused(self, tmp_path, hub):
        engine = _engine(tmp_path)
        assert engine.apply_bucket_policy("cloud:nosuch",
                                          SecurityMode.BALANCED)["success"] \
            is False

    def test_a_malformed_bucket_id_is_refused(self, tmp_path, hub):
        engine = _engine(tmp_path)
        for bad in ("", "ring", "cloud:", "cloud:ring; rm -rf /", "../../etc"):
            result = engine.apply_bucket_policy(bad, SecurityMode.BALANCED)
            assert result["success"] is False, bad


# ══════════════════════════════════════════════════════════════════════
#  4. Persistence, push, provenance, preview
# ══════════════════════════════════════════════════════════════════════

class TestPersistence:

    def test_the_ledger_survives_a_restart(self, tmp_path, hub):
        engine = _engine(tmp_path)
        engine.apply_bucket_policy(RING, SecurityMode.BALANCED,
                                   recorded_by="tool")
        path = tmp_path / "zone3_managed_buckets.json"
        assert path.exists(), "the bucket ledger lands next to the device one"
        rows = json.loads(path.read_text())
        assert rows[0]["bucket_id"] == RING
        assert rows[0]["recorded_by"] == "tool"

        reborn = _engine(tmp_path)
        again = reborn.get_bucket_policy(RING)
        assert again["mode"] == "balanced"
        assert again["client_ip"] == HUB

    def test_a_corrupt_ledger_does_not_take_zone3_down(self, tmp_path, hub):
        (tmp_path / "zone3_managed_buckets.json").write_text("{not json")
        engine = _engine(tmp_path)
        assert engine.get_managed_buckets() == []

    def test_re_recording_keeps_the_original_created_at(self, tmp_path, hub):
        engine = _engine(tmp_path)
        engine.apply_bucket_policy(RING, SecurityMode.OPEN)
        first = engine.get_bucket_policy(RING)["created_at"]
        engine.apply_bucket_policy(RING, SecurityMode.LOCKDOWN)
        assert engine.get_bucket_policy(RING)["created_at"] == first


class TestPushIsTheOwnersJob:

    def test_recording_does_not_touch_adguard(self, tmp_path, hub):
        adguard = RecordingAdGuard(enforce=True)
        engine = _engine(tmp_path, adguard=adguard)
        result = engine.apply_bucket_policy(RING, SecurityMode.LOCKDOWN,
                                            recorded_by="tool")
        assert result["pushed"] is False
        assert result["domains_blocked"] == 0
        assert result["domains_recorded"] > 0
        assert adguard.blocks == [], (
            "recording a bucket policy wrote to AdGuard")

    def test_push_true_writes_the_block(self, tmp_path, hub):
        adguard = RecordingAdGuard(enforce=True)
        engine = _engine(tmp_path, adguard=adguard)
        result = engine.apply_bucket_policy(RING, SecurityMode.BALANCED,
                                            push=True)
        assert result["pushed"] is True
        assert adguard.blocks, "push=True did not reach the controller"
        assert any(f"$client={HUB}" in ln for ln in adguard.last_block)

    def test_sync_dns_replays_the_bucket_ledger(self, tmp_path, hub):
        """A bucket recorded while observing is part of what a later sync
        pushes — the same property the device ledger has."""
        adguard = RecordingAdGuard(enforce=False)
        engine = _engine(tmp_path, adguard=adguard)
        engine.apply_bucket_policy(RING, SecurityMode.BALANCED)
        assert adguard.blocks == []
        out = engine.sync_dns()
        assert out["managed_buckets"] == 1
        assert any(f"$client={HUB}" in ln for ln in adguard.last_block)


class TestStatusAndPreview:

    def test_status_reports_managed_buckets(self, tmp_path, hub):
        engine = _engine(tmp_path)
        engine.apply_bucket_policy(RING, SecurityMode.BALANCED)
        status = engine.get_zone3_status()
        assert status["managed_bucket_count"] == 1
        assert status["managed_buckets"][0]["integration"] == "ring"
        assert status["bucket_recorded_domains"] > 0
        # Not enforcing: recorded is not blocked, and the two must not collapse.
        assert status["bucket_blocked_domains"] == 0

    def test_the_preview_lists_bucket_rows_with_device_counts(self, tmp_path,
                                                              hub):
        from puffpai.zone3.engine import enforce_preview
        engine = _engine(tmp_path)
        engine.apply_bucket_policy(RING, SecurityMode.LOCKDOWN)
        preview = enforce_preview(engine)
        assert preview["buckets"], "no bucket rows in the enforce proposal"
        row = preview["buckets"][0]
        assert row["integration"] == "ring"
        assert row["devices"] == 3
        assert row["breaks_integration"] is True
        assert preview["breaks_integrations"] == ["ring"]
        joined = "\n".join(preview["lines"])
        assert "3 device(s), via Home Assistant" in joined
        assert "THIS BREAKS THE INTEGRATION" in joined

    def test_a_tool_recorded_bucket_is_marked_in_the_preview(self, tmp_path,
                                                             hub):
        from puffpai.zone3.engine import enforce_preview
        engine = _engine(tmp_path)
        engine.apply_bucket_policy(RING, SecurityMode.BALANCED,
                                   recorded_by="tool")
        preview = enforce_preview(engine)
        assert preview["tool_recorded"] == 1
        assert "(recorded by a chat tool, not yet approved)" in \
            "\n".join(preview["lines"])

    def test_an_owner_recorded_bucket_is_not_marked(self, tmp_path, hub):
        from puffpai.zone3.engine import enforce_preview
        engine = _engine(tmp_path)
        engine.apply_bucket_policy(RING, SecurityMode.BALANCED,
                                   recorded_by="owner")
        preview = enforce_preview(engine)
        assert preview["tool_recorded"] == 0
        assert "recorded by a chat tool" not in "\n".join(preview["lines"])


class TestRemoval:

    def test_removing_a_bucket_policy_drops_its_rules(self, tmp_path, hub):
        adguard = RecordingAdGuard(enforce=True)
        engine = _engine(tmp_path, adguard=adguard)
        engine.apply_bucket_policy(RING, SecurityMode.LOCKDOWN, push=True)
        assert any("$client=" in ln for ln in adguard.last_block)
        engine.remove_bucket_policy(RING, push=True)
        assert not any("$client=" in ln and "ring" in ln
                       for ln in adguard.last_block)
        assert engine.get_managed_buckets() == []


class TestBucketPolicyDataclass:

    def test_unknown_keys_in_a_stored_row_are_dropped(self):
        """A ledger written by a future build must not crash this one."""
        row = BucketPolicy.from_dict(
            {"bucket_id": RING, "integration": "ring", "tier": "cloud",
             "mode": "balanced", "something_new": 1})
        assert row.bucket_id == RING
        assert row.security_mode is SecurityMode.BALANCED
