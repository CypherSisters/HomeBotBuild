""""Network scan & privacy": the firewall API must not bind a Windows backend
on a Linux machine.

The failure mode:

    GET /firewall/status
    -> {"backend":"WindowsFirewall","blocked_count":0,"lockdown_active":false}

on a Jetson Orin. Binding the back-compat alias ``FirewallManager``
(= ``WindowsFirewall``) instead of the platform factory makes every block /
unblock / lockdown drive ``netsh`` on a machine that has none.

What is asserted here:
  * the factory picks by platform and NEVER hands Linux a netsh backend;
  * Linux gets a refusing backend unless a host explicitly opts in;
  * a refused write changes nothing on disk and says so with a stable code;
  * the write routes answer 501, the read routes still work;
  * when the nftables backend IS opted into, the exact argv it emits is the
    one that adds/removes an element in PuffPAI's OWN table, and nothing it
    ever runs can touch any base rules the operator installed outside PuffPAI
    (INPUT, DOCKER-USER, the filter table, or any flush).
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Imported as a MODULE, never as bound names. Other test modules in this
# suite reload puffpai.firewall_manager through the patched_modules helper,
# and reload re-executes into the SAME module dict -- so a name bound here at
# import time becomes a stale class object while the factory hands back the
# fresh one, and every isinstance check silently inverts. Resolving through
# the module on each use makes these tests order-independent.
from puffpai import firewall_manager as fwm

NOT_AVAILABLE_CODE = "not_available_on_this_platform"

_ENV = "PUFFPAI_FIREWALL_BACKEND"


@pytest.fixture()
def blocked(tmp_path):
    p = tmp_path / "blocked.json"
    p.write_text("[]")
    return p


@pytest.fixture(autouse=True)
def _no_backend_optin(monkeypatch):
    """Every test starts from "this host has not opted in"."""
    monkeypatch.delenv(_ENV, raising=False)


# ── platform selection ────────────────────────────────────────────────────

class TestPlatformSelection:
    def test_windows_still_gets_netsh_backend(self, blocked):
        with patch.object(sys, "platform", "win32"):
            assert isinstance(fwm.get_firewall_backend(str(blocked)), fwm.WindowsFirewall)

    def test_linux_never_gets_the_windows_backend(self, blocked):
        """The exact defect: an ARM64 Linux box reporting backend WindowsFirewall."""
        with patch.object(sys, "platform", "linux"):
            fw = fwm.get_firewall_backend(str(blocked))
        assert not isinstance(fw, fwm.WindowsFirewall)
        assert fw.get_status()["backend"] != "WindowsFirewall"

    def test_linux_defaults_to_unavailable(self, blocked):
        with patch.object(sys, "platform", "linux"):
            fw = fwm.get_firewall_backend(str(blocked))
        assert isinstance(fw, fwm.UnavailableFirewall)
        assert fw.writes_supported is False

    @pytest.mark.parametrize("value", ["nft", "nftables", "NFTables", " nft "])
    def test_linux_opt_in_selects_nftables(self, blocked, monkeypatch, value):
        monkeypatch.setenv(_ENV, value)
        with patch.object(sys, "platform", "linux"):
            assert isinstance(fwm.get_firewall_backend(str(blocked)), fwm.NftablesFirewall)

    @pytest.mark.parametrize("value", ["", "iptables", "yes", "true", "junk"])
    def test_unrecognised_opt_in_fails_closed(self, blocked, monkeypatch, value):
        """An unknown value must not be read as "any backend will do"."""
        monkeypatch.setenv(_ENV, value)
        with patch.object(sys, "platform", "linux"):
            assert isinstance(fwm.get_firewall_backend(str(blocked)), fwm.UnavailableFirewall)

    def test_other_platform_refuses_rather_than_pretending(self, blocked):
        """darwin used to get MockFirewall, which reports success for a block
        that never happened — the one answer a security control must not give."""
        with patch.object(sys, "platform", "darwin"):
            fw = fwm.get_firewall_backend(str(blocked))
        assert isinstance(fw, fwm.UnavailableFirewall)

    def test_unavailable_is_a_real_backend(self):
        assert issubclass(fwm.UnavailableFirewall, fwm.FirewallBackend)


# ── the refusing backend ──────────────────────────────────────────────────

class TestUnavailableBackend:
    def test_block_is_refused_and_persists_nothing(self, blocked):
        fw = fwm.UnavailableFirewall(str(blocked))
        result = fw.block_device("203.0.113.7", "regression test")
        assert result["success"] is False
        assert result["code"] == NOT_AVAILABLE_CODE
        # Fail closed: no phantom entry claiming the device is blocked.
        assert fw.blocked_devices == []
        assert json.loads(blocked.read_text()) == []

    def test_unblock_is_refused(self, blocked):
        blocked.write_text(json.dumps(
            [{"ip": "203.0.113.7", "rule_name": "PuffPAI_Block_203_0_113_7"}]))
        fw = fwm.UnavailableFirewall(str(blocked))
        result = fw.unblock_device("203.0.113.7")
        assert result["success"] is False
        assert result["code"] == NOT_AVAILABLE_CODE
        # And the list is NOT quietly emptied, which would read as "unblocked".
        assert json.loads(blocked.read_text())[0]["ip"] == "203.0.113.7"

    def test_lockdown_is_refused_and_not_marked_active(self, blocked):
        fw = fwm.UnavailableFirewall(str(blocked))
        result = fw.lockdown_mode(True)
        assert result["success"] is False
        assert result["code"] == NOT_AVAILABLE_CODE
        assert fw.get_status()["lockdown_active"] is False

    def test_add_rule_is_refused_not_saved_as_pending(self, blocked, tmp_path,
                                                     monkeypatch):
        """The base class saves a failed rule as 'pending'. Here nothing will
        ever enforce it, so a saved rule would be a standing false claim."""
        monkeypatch.setattr("puffpai.firewall_manager._RULES_FILE",
                            str(tmp_path / "rules.json"))
        fw = fwm.UnavailableFirewall(str(blocked))
        result = fw.add_rule("test", "outbound", "block", "TCP", "443")
        assert result["success"] is False
        assert result["code"] == NOT_AVAILABLE_CODE
        assert fw.get_rules() == []
        assert not (tmp_path / "rules.json").exists()

    def test_delete_rule_reports_failure(self, blocked):
        assert fwm.UnavailableFirewall(str(blocked)).delete_rule("rule_x") is False

    def test_status_says_unavailable_with_a_reason(self, blocked):
        fw = fwm.UnavailableFirewall(str(blocked), reason="Because of X.")
        status = fw.get_status()
        assert status["available"] is False
        assert status["code"] == NOT_AVAILABLE_CODE
        assert status["reason"] == "Because of X."

    def test_working_backend_reports_available(self, blocked):
        """The flag must distinguish "nothing blocked" from "cannot block"."""
        assert fwm.WindowsFirewall(str(blocked)).get_status()["available"] is True

    def test_reason_never_leaks_a_path_or_token(self, blocked):
        with patch.object(sys, "platform", "linux"):
            fw = fwm.get_firewall_backend(str(blocked))
        reason = fw.get_status()["reason"]
        assert str(blocked) not in reason
        assert "/opt/puffpai" not in reason
        assert "\\" not in reason


# ── a fake nft runner: exact argv, and the base rules are untouchable ─────

class FakeNft:
    """A tiny nft stand-in that records argv and keeps the element sets, so a
    block can be READ BACK rather than inferred from a zero exit code."""

    #: Anything naming base rules the operator installed outside PuffPAI.
    FORBIDDEN = ("INPUT", "OUTPUT", "FORWARD", "DOCKER-USER", "flush",
                 "iptables", "iptables-legacy", "ip6tables", "ip6tables-legacy")

    def __init__(self):
        self.calls: list[list] = []
        self.sets: dict = {"blocked_v4": set(), "blocked_v6": set()}

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        # add/delete element inet puffpai <set> { <ip> }
        if len(cmd) > 5 and cmd[1] in ("add", "delete") and cmd[2] == "element":
            set_name, ip = cmd[4], cmd[6]
            if cmd[1] == "add":
                self.sets[set_name].add(ip)
            else:
                self.sets[set_name].discard(ip)
        return MagicMock(returncode=0, stdout="", stderr="")

    def assert_base_rules_untouched(self):
        for call in self.calls:
            assert call[0] == "nft", f"non-nft command issued: {call}"
            for token in call:
                for bad in self.FORBIDDEN:
                    assert bad not in str(token), (
                        f"command would reach the base firewall: {call}")


@pytest.fixture()
def fake_nft(monkeypatch):
    fake = FakeNft()
    monkeypatch.setattr("subprocess.run", fake)
    return fake


class TestNftablesCommandsAreScopedToOurOwnTable:
    def test_block_emits_exactly_the_add_element_command(self, blocked, fake_nft):
        fw = fwm.NftablesFirewall(str(blocked))
        assert fw.block_device("203.0.113.7")["success"] is True
        assert fake_nft.calls == [
            ["nft", "add", "element", "inet puffpai", "blocked_v4",
             "{", "203.0.113.7", "}"],
        ]

    def test_block_can_be_read_back_from_the_set(self, blocked, fake_nft):
        fw = fwm.NftablesFirewall(str(blocked))
        fw.block_device("203.0.113.7")
        # Read-back: the element the emitted command would create is present.
        assert "203.0.113.7" in fake_nft.sets["blocked_v4"]

    def test_unblock_removes_the_element_again(self, blocked, fake_nft):
        fw = fwm.NftablesFirewall(str(blocked))
        fw.block_device("203.0.113.7")
        fake_nft.calls.clear()
        assert fw.unblock_device("203.0.113.7")["success"] is True
        assert fake_nft.calls == [
            ["nft", "delete", "element", "inet puffpai", "blocked_v4",
             "{", "203.0.113.7", "}"],
        ]
        assert "203.0.113.7" not in fake_nft.sets["blocked_v4"]

    def test_ipv6_goes_to_the_v6_set(self, blocked, fake_nft):
        fw = fwm.NftablesFirewall(str(blocked))
        fw.block_device("2001:db8::7")
        assert fake_nft.calls[0][4] == "blocked_v6"
        assert "2001:db8::7" in fake_nft.sets["blocked_v6"]

    def test_block_unblock_never_touches_the_base_chains(self, blocked, fake_nft):
        fw = fwm.NftablesFirewall(str(blocked))
        fw.block_device("203.0.113.7")
        fw.unblock_device("203.0.113.7")
        fw.lockdown_mode(True)
        fw.ensure_table()
        fake_nft.assert_base_rules_untouched()

    def test_every_command_names_the_puffpai_table(self, blocked, fake_nft):
        fw = fwm.NftablesFirewall(str(blocked))
        fw.block_device("203.0.113.7")
        fw.ensure_table()
        for call in fake_nft.calls:
            assert "inet puffpai" in call, call


# ── the API routes ────────────────────────────────────────────────────────

@pytest.fixture()
def api(monkeypatch, tmp_path):
    """api_server with its module-level backend swapped for the refusing one."""
    api_server = pytest.importorskip("puffpai.api_server")
    bf = tmp_path / "blocked.json"
    bf.write_text("[]")
    monkeypatch.setattr(api_server, "fw",
                        fwm.UnavailableFirewall(str(bf), reason="No backend here."))
    monkeypatch.setattr(api_server, "_fw_backend", None, raising=False)
    api_server.app.config["TESTING"] = True
    with api_server.app.test_client() as client:
        yield client, api_server, bf


def _auth(api_server):
    key = getattr(api_server, "_API_KEY", None)
    return {"X-API-Key": key} if key else {}


class TestFirewallRoutesAreHonest:
    @pytest.mark.parametrize("path,payload", [
        ("/firewall/block", {"ip": "203.0.113.7"}),
        ("/firewall/unblock", {"ip": "203.0.113.7"}),
        ("/firewall/lockdown", {"enable": True}),
    ])
    def test_write_routes_return_501_with_a_stable_code(self, api, path, payload):
        client, api_server, bf = api
        resp = client.post(path, json=payload, headers=_auth(api_server))
        assert resp.status_code == 501, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["success"] is False
        assert body["code"] == NOT_AVAILABLE_CODE
        assert body["message"]
        # And nothing was written.
        assert json.loads(bf.read_text()) == []

    def test_status_route_still_answers_and_says_unavailable(self, api):
        client, api_server, _ = api
        resp = client.get("/firewall/status", headers=_auth(api_server))
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["available"] is False
        assert body["backend"] == "UnavailableFirewall"
        assert body["code"] == NOT_AVAILABLE_CODE


# ── what the capability read now says ─────────────────────────────────────

class TestCapabilityReadStopsAdvertisingTheBlock:
    def test_security_network_reports_unavailable_on_this_platform(self):
        from puffpai import capability_probes, capability_registry

        capability_probes.reset_cache()
        with patch.object(sys, "platform", "linux"), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop(_ENV, None)
            cap = capability_registry.get("security.network")
            available, reason = capability_registry.runtime_availability(cap)
        capability_probes.reset_cache()
        assert available is False
        assert reason

    def test_the_scan_half_is_not_disowned(self):
        """security.network's authority tool is the ARP scan, which works here.
        Only the write half is unavailable, so the STATUS stays live."""
        from puffpai import capability_probes, capability_registry

        capability_probes.reset_cache()
        with patch.object(sys, "platform", "linux"):
            os.environ.pop(_ENV, None)
            cap = capability_registry.get("security.network")
            assert capability_registry.runtime_status(cap) == capability_registry.LIVE
        capability_probes.reset_cache()

    def test_spoken_security_area_names_what_is_missing(self):
        from puffpai import capability_probes, capability_registry

        capability_probes.reset_cache()
        with patch.object(sys, "platform", "linux"):
            os.environ.pop(_ENV, None)
            text = capability_registry.describe("security", for_voice=True)
        capability_probes.reset_cache()
        assert "Network scan & privacy" in text
        # The line must carry a caveat, not a bare claim.
        line = next(ln for ln in text.splitlines() if "Network scan" in ln)
        assert "(" in line
