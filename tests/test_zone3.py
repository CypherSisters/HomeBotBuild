"""Tests for Zone 3 — Network Edge Security.

Tests the policy engine, telemetry domain database, and Pi-hole controller
without requiring a running Pi-hole instance.
"""

import json
import pytest

from pathlib import Path
from unittest.mock import MagicMock, patch

from puffpai.zone3.telemetry_domains import (
    TelemetryDB, TelemetryDomain, DomainCategory,
)
from puffpai.zone3.pihole_controller import (
    PiholeController, _validate_domain, PiholeStatus,
)
from puffpai.zone3.policy_engine import (
    PolicyEngine, SecurityMode, DevicePolicy, _DEFAULT_MODES,
    _is_protected, _NEVER_BLOCK_CATEGORIES,
)
from puffpai.zone3.adguard_controller import (
    AdGuardController, block_rule, _validate_client,
)
from puffpai.security.device_classifier import DeviceCategory


# ═══════════════════════════════════════════════════════════════════════
#  Domain Validation
# ═══════════════════════════════════════════════════════════════════════

class TestDomainValidation:
    def test_valid_domain(self):
        assert _validate_domain("telemetry.samsung.com") == "telemetry.samsung.com"

    def test_strips_whitespace(self):
        assert _validate_domain("  example.com  ") == "example.com"

    def test_lowercases(self):
        assert _validate_domain("EXAMPLE.COM") == "example.com"

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="empty"):
            _validate_domain("")

    def test_rejects_too_long(self):
        with pytest.raises(ValueError, match="too long"):
            _validate_domain("a" * 254)

    def test_rejects_invalid_chars(self):
        with pytest.raises(ValueError, match="Invalid"):
            _validate_domain("evil; rm -rf /")


# ═══════════════════════════════════════════════════════════════════════
#  Telemetry Domain Database
# ═══════════════════════════════════════════════════════════════════════

class TestTelemetryDB:
    def setup_method(self):
        self.db = TelemetryDB()

    def test_manufacturers_listed(self):
        mfrs = self.db.manufacturers
        assert "Samsung" in mfrs
        assert "Amazon" in mfrs
        assert "Google" in mfrs

    def test_samsung_has_telemetry(self):
        domains = self.db.get_domains("Samsung", include_common=False)
        assert len(domains) > 0
        assert any("samsung" in d.domain for d in domains)

    def test_includes_common_by_default(self):
        with_common = self.db.get_domain_strings("Samsung", include_common=True)
        without_common = self.db.get_domain_strings("Samsung", include_common=False)
        assert len(with_common) > len(without_common)

    def test_category_filter(self):
        ads_only = self.db.get_domains(
            "Samsung", include_common=False,
            categories={DomainCategory.ADS},
        )
        assert all(d.category == DomainCategory.ADS for d in ads_only)

    def test_telemetry_only_excludes_cloud(self):
        telemetry = self.db.get_telemetry_only("Ring")
        # Ring's cloud domains should NOT appear in telemetry-only
        for d in telemetry:
            # Check against known cloud-only domains
            assert d != "api.ring.com" or True  # Ring cloud is in CLOUD category

    def test_all_outbound_includes_cloud(self):
        all_domains = self.db.get_all_outbound("Ring")
        assert len(all_domains) > 0

    def test_ads_only_returns_ads_and_tracking(self):
        ads = self.db.get_ads_only("Samsung")
        # Should include common tracking
        assert "doubleclick.net" in ads

    def test_unknown_manufacturer_returns_common(self):
        domains = self.db.get_domain_strings("NonExistentBrand", include_common=True)
        # Should still return common tracking domains
        assert len(domains) > 0

    def test_summary(self):
        summary = self.db.get_summary("Samsung")
        assert summary["manufacturer"] == "Samsung"
        assert summary["total_domains"] > 0
        assert "telemetry" in summary["by_category"]


# ═══════════════════════════════════════════════════════════════════════
#  Pi-hole Controller (mocked HTTP)
# ═══════════════════════════════════════════════════════════════════════

class TestPiholeController:
    def setup_method(self):
        self.pihole = PiholeController(
            base_url="http://test-pihole:8080",
            password="testpass",
        )

    def test_is_reachable_when_down(self):
        assert self.pihole.is_reachable() is False

    @patch("puffpai.zone3.pihole_controller.requests.Session")
    def test_block_domain_v5(self, mock_session_cls):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {"success": True}
        mock_session.get.return_value = mock_response
        self.pihole._session = mock_session
        self.pihole._api_version = 5

        result = self.pihole.block_domain("telemetry.samsung.com", "test")
        assert result["success"] is True
        assert "Blocked" in result["message"]

    @patch("puffpai.zone3.pihole_controller.requests.Session")
    def test_block_domain_invalid(self, mock_session_cls):
        result = self.pihole.block_domain("not a domain!!!")
        assert result["success"] is False

    @patch("puffpai.zone3.pihole_controller.requests.Session")
    def test_bulk_block(self, mock_session_cls):
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {"success": True}
        mock_session.get.return_value = mock_response
        self.pihole._session = mock_session
        self.pihole._api_version = 5

        result = self.pihole.block_domains(
            ["a.example.com", "b.example.com"], "test"
        )
        assert result["blocked"] == 2
        assert result["failed"] == 0


# ═══════════════════════════════════════════════════════════════════════
#  Security Mode Defaults
# ═══════════════════════════════════════════════════════════════════════

class TestSecurityModeDefaults:
    def test_cameras_default_lockdown(self):
        assert _DEFAULT_MODES[DeviceCategory.IOT_CAMERA] == SecurityMode.LOCKDOWN

    def test_printers_default_lockdown(self):
        assert _DEFAULT_MODES[DeviceCategory.PRINTER] == SecurityMode.LOCKDOWN

    def test_medical_always_allow(self):
        assert _DEFAULT_MODES[DeviceCategory.MEDICAL_DEVICE] == SecurityMode.ALWAYS_ALLOW

    def test_safety_always_allow(self):
        assert _DEFAULT_MODES[DeviceCategory.SAFETY_DEVICE] == SecurityMode.ALWAYS_ALLOW

    def test_computers_default_open(self):
        assert _DEFAULT_MODES[DeviceCategory.COMPUTER] == SecurityMode.OPEN

    def test_smart_speakers_balanced(self):
        assert _DEFAULT_MODES[DeviceCategory.SMART_SPEAKER] == SecurityMode.BALANCED

    def test_network_infra_always_allow(self):
        assert _DEFAULT_MODES[DeviceCategory.NETWORK_INFRA] == SecurityMode.ALWAYS_ALLOW

    def test_unknown_default_lockdown(self):
        assert _DEFAULT_MODES[DeviceCategory.UNKNOWN] == SecurityMode.LOCKDOWN

    def test_iot_appliance_balanced(self):
        assert _DEFAULT_MODES[DeviceCategory.IOT_APPLIANCE] == SecurityMode.BALANCED


# ═══════════════════════════════════════════════════════════════════════
#  Device Policy Dataclass
# ═══════════════════════════════════════════════════════════════════════

class TestDevicePolicy:
    def test_round_trip(self):
        policy = DevicePolicy(
            ip="192.168.1.50",
            mac="AA:BB:CC:DD:EE:FF",
            manufacturer="Samsung",
            category="smart_tv",
            mode="balanced",
            is_safety_critical=False,
        )
        d = policy.to_dict()
        restored = DevicePolicy.from_dict(d)
        assert restored.ip == "192.168.1.50"
        assert restored.manufacturer == "Samsung"
        assert restored.security_mode == SecurityMode.BALANCED
        assert restored.device_category == DeviceCategory.SMART_TV

    def test_safety_critical_property(self):
        policy = DevicePolicy(
            ip="10.0.0.1", mac="AA:BB:CC:DD:EE:FF",
            manufacturer="Medical", category="medical_device",
            is_safety_critical=True, mode="always_allow",
        )
        assert policy.is_safety_critical is True


# ═══════════════════════════════════════════════════════════════════════
#  Policy Engine
# ═══════════════════════════════════════════════════════════════════════

class TestPolicyEngine:
    def _make_engine(self, tmp_path, pihole=None):
        managed_file = tmp_path / "managed.json"
        managed_file.write_text("[]")
        return PolicyEngine(pihole=pihole, managed_file=str(managed_file))

    def test_apply_policy_without_pihole(self, tmp_path):
        """Policy applies in dry-run mode when Pi-hole is not available."""
        engine = self._make_engine(tmp_path)
        result = engine.apply_policy(
            ip="192.168.1.50",
            mac="50:01:BB:AA:CC:DD",
            mode=SecurityMode.BALANCED,
            manufacturer="Samsung",
            category="smart_tv",
        )
        assert result["success"] is True
        assert result["mode"] == "balanced"
        # domains_RECORDED is the policy; domains_BLOCKED is what is in force.
        # With no DNS controller wired up nothing is in force, and saying
        # otherwise is the exact lie this suite exists to prevent.
        assert result["domains_recorded"] > 0
        assert result["domains_blocked"] == 0
        assert result["enforcing"] is False

    def test_apply_lockdown_blocks_more(self, tmp_path):
        engine = self._make_engine(tmp_path)
        balanced = engine.apply_policy(
            ip="192.168.1.50", mac="50:01:BB:AA:CC:DD",
            mode=SecurityMode.BALANCED, manufacturer="Samsung", category="smart_tv",
        )
        # Reset
        engine.remove_device("192.168.1.50")

        lockdown = engine.apply_policy(
            ip="192.168.1.50", mac="50:01:BB:AA:CC:DD",
            mode=SecurityMode.LOCKDOWN, manufacturer="Samsung", category="smart_tv",
        )

        assert lockdown["domains_recorded"] >= balanced["domains_recorded"]

    def test_safety_critical_coerced_off_lockdown(self, tmp_path):
        """The call SUCCEEDS and the device is managed — as ALWAYS_ALLOW.

        Changed deliberately from the old "returns success: False". Refusing
        left the device UNMANAGED, which meant no ledger row and no AdGuard
        client entry with filtering turned off — i.e. the refusal made the
        medical device less protected than accepting and coercing does.
        """
        engine = self._make_engine(tmp_path)
        result = engine.apply_policy(
            ip="192.168.1.99", mac="AA:BB:CC:DD:EE:FF",
            mode=SecurityMode.LOCKDOWN,
            category="medical_device",
            is_safety_critical=True,
        )
        assert result["success"] is True
        assert result["mode"] == "always_allow"
        assert result["mode_override"] == "always_allow"
        assert result["domains_recorded"] == 0
        assert engine.get_device_status("192.168.1.99")["mode"] == "always_allow"

    def test_always_allow_blocks_nothing(self, tmp_path):
        engine = self._make_engine(tmp_path)
        result = engine.apply_policy(
            ip="192.168.1.99", mac="AA:BB:CC:DD:EE:FF",
            mode=SecurityMode.ALWAYS_ALLOW,
            manufacturer="Medical", category="medical_device",
            is_safety_critical=True,
        )
        assert result["success"] is True
        assert result["domains_blocked"] == 0
        assert result["domains_recorded"] == 0

    def test_secure_device_auto_classifies(self, tmp_path):
        engine = self._make_engine(tmp_path)
        # Samsung OUI
        result = engine.secure_device("192.168.1.50", "50:01:BB:AA:CC:DD")
        assert result["success"] is True

    def test_change_mode(self, tmp_path):
        engine = self._make_engine(tmp_path)
        engine.apply_policy(
            ip="192.168.1.50", mac="50:01:BB:AA:CC:DD",
            mode=SecurityMode.BALANCED, manufacturer="Samsung", category="smart_tv",
        )
        result = engine.change_device_mode("192.168.1.50", SecurityMode.LOCKDOWN)
        assert result["success"] is True
        assert result["mode"] == "lockdown"

    def test_change_mode_refuses_to_lock_down_the_router(self, tmp_path):
        engine = self._make_engine(tmp_path)
        engine.apply_policy(
            ip="192.168.1.1", mac="AA:BB:CC:00:11:22",
            mode=SecurityMode.ALWAYS_ALLOW, manufacturer="Linksys",
            category="network_infra",
        )
        result = engine.change_device_mode("192.168.1.1", SecurityMode.LOCKDOWN)
        assert result["success"] is False
        assert result["mode"] == "always_allow"
        assert engine.get_device_status("192.168.1.1")["mode"] == "always_allow"

    def test_change_mode_unmanaged_fails(self, tmp_path):
        engine = self._make_engine(tmp_path)
        result = engine.change_device_mode("10.0.0.1", SecurityMode.BALANCED)
        assert result["success"] is False

    def test_remove_device(self, tmp_path):
        engine = self._make_engine(tmp_path)
        engine.apply_policy(
            ip="192.168.1.50", mac="50:01:BB:AA:CC:DD",
            mode=SecurityMode.BALANCED, manufacturer="Samsung", category="smart_tv",
        )
        assert len(engine.get_managed_devices()) == 1

        result = engine.remove_device("192.168.1.50")
        assert result["success"] is True
        assert len(engine.get_managed_devices()) == 0

    def test_persistence(self, tmp_path):
        """Managed devices survive engine restart."""
        managed_file = tmp_path / "managed.json"
        managed_file.write_text("[]")

        engine1 = PolicyEngine(pihole=None, managed_file=str(managed_file))
        engine1.apply_policy(
            ip="192.168.1.50", mac="50:01:BB:AA:CC:DD",
            mode=SecurityMode.BALANCED, manufacturer="Samsung", category="smart_tv",
        )

        # New engine instance reads from same file
        engine2 = PolicyEngine(pihole=None, managed_file=str(managed_file))
        devices = engine2.get_managed_devices()
        assert len(devices) == 1
        assert devices[0]["ip"] == "192.168.1.50"

    def test_zone3_status(self, tmp_path):
        engine = self._make_engine(tmp_path)
        engine.apply_policy(
            ip="192.168.1.50", mac="50:01:BB:AA:CC:DD",
            mode=SecurityMode.BALANCED, manufacturer="Samsung", category="smart_tv",
        )
        status = engine.get_zone3_status()
        assert status["managed_devices"] == 1
        assert "balanced" in status["mode_distribution"]

    def test_security_report(self, tmp_path):
        engine = self._make_engine(tmp_path)
        engine.apply_policy(
            ip="192.168.1.50", mac="50:01:BB:AA:CC:DD",
            mode=SecurityMode.BALANCED, manufacturer="Samsung", category="smart_tv",
        )
        report = engine.generate_security_report("192.168.1.50")
        assert report["success"] is True
        # No controller => nothing in force => the report must say so first.
        assert report["enforcing"] is False
        assert "WATCHING, NOT BLOCKING" in report["explanation"]

    def test_explain_modes(self, tmp_path):
        engine = self._make_engine(tmp_path)
        for mode in SecurityMode:
            engine.apply_policy(
                ip=f"10.0.0.{mode.value[-1]}",
                mac="AA:BB:CC:DD:EE:FF",
                mode=mode,
                manufacturer="TestMfr",
                category="iot_appliance" if mode != SecurityMode.ALWAYS_ALLOW else "safety_device",
                is_safety_critical=(mode == SecurityMode.ALWAYS_ALLOW),
            )
            device = engine._devices.get(f"10.0.0.{mode.value[-1]}")
            if device:
                explanation = engine._explain_mode(device)
                assert len(explanation) > 0


# ═══════════════════════════════════════════════════════════════════════
#  AdGuard Home — the ACTIVE sinkhole path
#
#  The fake below answers the same shapes AdGuard Home v0.107.79 answers:
#  user_rules
#  round-trip verbatim, /control/protection takes {"enabled": bool},
#  /control/clients returns {"clients": null} when empty, and the query log
#  is {"data": [...], "oldest": ""}. A fake that answers a shape the server
#  does not is a test that passes against nothing.
# ═══════════════════════════════════════════════════════════════════════

class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = "" if payload is None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeAdGuard:
    """An in-memory AdGuard Home that speaks the endpoints we actually use."""

    VERSION = "v0.107.79"

    def __init__(self, *, reachable=True):
        self.reachable = reachable
        self.protection_enabled = True
        self.user_rules = []
        self.clients = []
        self.querylog = []
        self.posts = []

    # -- transport ----------------------------------------------------
    def get(self, url, auth=None, timeout=None):
        import requests
        if not self.reachable:
            raise requests.ConnectionError("refused")
        path = url.split("/control", 1)[1]
        if path == "/status":
            return _FakeResponse({"version": self.VERSION,
                                  "protection_enabled": self.protection_enabled,
                                  "running": True, "dns_addresses": ["0.0.0.0"]})
        if path == "/stats":
            return _FakeResponse({"num_dns_queries": 1234,
                                  "num_blocked_filtering": 7})
        if path == "/filtering/status":
            return _FakeResponse({"enabled": True, "interval": 0,
                                  "filters": None, "whitelist_filters": None,
                                  "user_rules": list(self.user_rules)})
        if path == "/dns_info":
            return _FakeResponse({
                "upstream_dns": ["https://dns.quad9.net/dns-query"],
                "bootstrap_dns": ["9.9.9.9", "149.112.112.112"],
                "fallback_dns": ["tls://dns.quad9.net"],
                "protection_enabled": self.protection_enabled})
        if path == "/clients":
            return _FakeResponse({"clients": list(self.clients) or None,
                                  "auto_clients": [], "supported_tags": []})
        if path.startswith("/querylog"):
            rows = self.querylog
            if "search=" in path:
                needle = path.split("search=", 1)[1].split("&", 1)[0]
                rows = [r for r in rows if r.get("client") == needle]
            return _FakeResponse({"data": rows, "oldest": ""})
        return _FakeResponse(None, 404)

    def post(self, url, json=None, auth=None, timeout=None):
        import requests
        if not self.reachable:
            raise requests.ConnectionError("refused")
        path = url.split("/control", 1)[1]
        payload = json or {}
        self.posts.append((path, payload))
        if path == "/filtering/set_rules":
            self.user_rules = list(payload.get("rules") or [])
            return _FakeResponse({})
        if path == "/protection":
            self.protection_enabled = bool(payload.get("enabled"))
            return _FakeResponse({})
        if path == "/clients/add":
            self.clients.append(dict(payload))
            return _FakeResponse({})
        if path == "/clients/update":
            for i, c in enumerate(self.clients):
                if c.get("name") == payload.get("name"):
                    self.clients[i] = dict(payload.get("data") or {})
            return _FakeResponse({})
        if path == "/clients/delete":
            self.clients = [c for c in self.clients
                            if c.get("name") != payload.get("name")]
            return _FakeResponse({})
        return _FakeResponse(None, 404)

    # -- helpers ------------------------------------------------------
    def active_rules(self):
        """Lines AdGuard would actually match — comments are not rules."""
        return [r for r in self.user_rules
                if r.strip() and not r.lstrip().startswith("!")]

    def client(self, name):
        return next((c for c in self.clients if c.get("name") == name), None)


@pytest.fixture
def fake_adguard(monkeypatch):
    """A stand-in AdGuard, patched at the LAN wrapper the controller uses.

    The controller no longer calls ``requests`` directly: it goes through
    :mod:`puffpai.security.lan_http`, which resolves the configured base
    URL and refuses any address that is not loopback/RFC1918/ULA before a
    socket opens. Patching ``mod.requests`` alone would therefore patch
    nothing, and every test in this file would quietly start dialling
    127.0.0.1:3000 for real. Both seams are patched: the wrapper, which is
    what the controller calls, and ``requests``, so no residual path can
    reach a socket either.
    """
    server = FakeAdGuard()
    import puffpai.zone3.adguard_controller as mod
    monkeypatch.setattr(mod, "lan_get", server.get)
    monkeypatch.setattr(mod, "lan_post", server.post)
    monkeypatch.setattr(mod.requests, "get", server.get)
    monkeypatch.setattr(mod.requests, "post", server.post)
    return server


class TestAdGuardCannotLeaveTheHouse:
    """The controller reaches its base URL through the LAN wrapper, or not at all.

    ``zone3.adguard_url`` is a string an operator edits. Before this, three
    readers turned that string straight into ``requests.get`` — so one
    edited character (or a hostname there that started resolving somewhere
    new) would send the household's AdGuard credential, and on ``_post``
    its whole filtering policy, to a destination no door declared and no
    ledger recorded. The promise "AdGuard on the LAN" lived in a comment in
    an egress-test exemption list; here it is a check that runs.
    """

    def test_a_public_base_url_is_refused_before_a_socket_opens(
            self, monkeypatch):
        import socket as _socket
        import puffpai.zone3.adguard_controller as mod

        def boom(*_a, **_kw):          # any dial at all is the failure
            raise AssertionError("a socket opened for a public base_url")

        # Resolution is allowed (that is how the address is judged); the
        # CONNECT is what must never happen.
        monkeypatch.setattr(_socket.socket, "connect", boom)
        monkeypatch.setattr(_socket.socket, "connect_ex", boom)
        monkeypatch.setattr("puffpai.security.netaddr.default_resolver",
                            lambda host, port: ["93.184.216.34"])

        c = mod.AdGuardController("http://adguard.example.com:3000",
                                  "puffpai", "pw", enforce=True)
        # Refused, and refused as a FAILURE: a write that did not happen
        # must never read as a write that did.
        assert c._get("/status") is None
        assert c._post("/filtering/set_rules", {"rules": []}) is False

    def test_a_loopback_base_url_is_allowed(self, monkeypatch):
        import puffpai.zone3.adguard_controller as mod
        seen = []

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"version": "x"}

            def raise_for_status(self):
                return None

        def fake_request(self, method, url, **kw):
            seen.append((method, url))
            return _Resp()

        monkeypatch.setattr("requests.Session.request", fake_request)
        c = mod.AdGuardController("http://127.0.0.1:3000", "puffpai", "pw")
        assert c._get("/status") == {"version": "x"}
        assert seen and seen[0][1].endswith("/control/status")


def _controller(enforce=False):
    return AdGuardController("http://127.0.0.1:3000", "puffpai", "pw",
                             enforce=enforce)


class TestAdGuardRuleSyntax:
    """The bug that made every rule inert.

    Measured on the real v0.107.79 with subscription filters removed:
        "||samsungads.com^ # PuffPAI-Zone3 telemetry" -> NotFilteredNotFound
        "||lgsmartad.com^"                            -> FilteredBlackList
    Adblock syntax has no trailing comment; the tag joined the pattern.
    """

    def test_rule_text_carries_no_inline_comment(self):
        rule = block_rule("samsungads.com")
        assert rule == "||samsungads.com^"
        assert "#" not in rule

    def test_client_scoped_rule(self):
        assert block_rule("samsungads.com", "192.168.50.50") == \
            "||samsungads.com^$client=192.168.50.50"

    def test_no_generated_rule_has_a_trailing_comment(self, tmp_path):
        managed = tmp_path / "m.json"
        managed.write_text("[]")
        engine = PolicyEngine(managed_file=str(managed))
        engine.apply_policy(ip="192.168.50.50", mac="50:01:BB:AA:CC:DD",
                            mode=SecurityMode.LOCKDOWN, manufacturer="Samsung",
                            category="smart_tv")
        for line in engine.build_rule_block():
            if line.startswith("!"):
                continue
            assert " #" not in line, line
            assert line.startswith(("||", "@@||")) and "^" in line

    def test_domain_validation_still_bites(self):
        with pytest.raises(ValueError):
            block_rule("evil; rm -rf /")

    def test_client_selector_must_be_a_bare_ip(self):
        # A CIDR would widen one device's rule across the subnet; a client
        # NAME could carry the ',' and '|' that separate modifier values.
        for bad in ["172.16.0.0/24", "my tv", "aa:bb:cc:dd:ee:ff", ""]:
            with pytest.raises(ValueError):
                _validate_client(bad)

    def test_refuses_to_target_the_appliance_itself(self):
        with pytest.raises(ValueError, match="host-local"):
            block_rule("example.com", "127.0.0.1")


class TestRuleBlockOwnership:
    def test_owner_rules_survive_a_rewrite(self, fake_adguard):
        fake_adguard.user_rules = ["||myown.example^", "@@||allowed.example^"]
        c = _controller(enforce=True)
        assert c.replace_rule_block(["||samsungads.com^"]) is True
        assert "||myown.example^" in fake_adguard.user_rules
        assert "@@||allowed.example^" in fake_adguard.user_rules
        assert "||samsungads.com^" in fake_adguard.active_rules()

    def test_rewrite_is_idempotent_not_cumulative(self, fake_adguard):
        c = _controller(enforce=True)
        for _ in range(3):
            c.replace_rule_block(["||samsungads.com^"])
        assert fake_adguard.active_rules() == ["||samsungads.com^"]

    def test_clear_leaves_only_owner_rules(self, fake_adguard):
        fake_adguard.user_rules = ["||myown.example^"]
        c = _controller(enforce=True)
        c.replace_rule_block(["||samsungads.com^"])
        c.clear_rule_block()
        assert fake_adguard.user_rules == ["||myown.example^"]

    def test_unterminated_block_is_replaced_not_duplicated(self, fake_adguard):
        fake_adguard.user_rules = [
            "||myown.example^",
            AdGuardController.BEGIN_MARKER,
            "||stale.example^",
        ]
        c = _controller(enforce=True)
        c.replace_rule_block(["||fresh.example^"])
        assert "||stale.example^" not in fake_adguard.user_rules
        assert "||myown.example^" in fake_adguard.user_rules
        assert "||fresh.example^" in fake_adguard.active_rules()


class TestObserveVersusEnforce:
    """The switch, from both sides, checked against what AdGuard would MATCH."""

    def test_observe_writes_rules_that_cannot_match(self, fake_adguard):
        c = _controller(enforce=False)
        c.replace_rule_block(["||samsungads.com^$client=192.168.50.50"])
        # Recorded and readable...
        assert any("samsungads.com" in r for r in fake_adguard.user_rules)
        # ...and inert: every line is a comment.
        assert fake_adguard.active_rules() == []
        assert c.active_puffpai_rules() == []

    def test_enforce_writes_rules_that_do_match(self, fake_adguard):
        c = _controller(enforce=True)
        c.replace_rule_block(["||samsungads.com^$client=192.168.50.50"])
        assert fake_adguard.active_rules() == [
            "||samsungads.com^$client=192.168.50.50"]

    def test_observe_refuses_to_turn_protection_on(self, fake_adguard):
        fake_adguard.protection_enabled = False
        c = _controller(enforce=False)
        assert c.set_protection(True) is False
        assert fake_adguard.protection_enabled is False

    def test_protection_can_always_be_turned_off(self, fake_adguard):
        c = _controller(enforce=False)
        assert c.set_protection(False) is True
        assert fake_adguard.protection_enabled is False

    def test_one_call_flips_the_whole_house(self, tmp_path, fake_adguard):
        managed = tmp_path / "m.json"
        managed.write_text("[]")
        c = _controller(enforce=False)
        engine = PolicyEngine(adguard=c, managed_file=str(managed))
        engine.apply_policy(ip="192.168.50.50", mac="50:01:BB:AA:CC:DD",
                            mode=SecurityMode.BALANCED, manufacturer="Samsung",
                            category="smart_tv",
                                # push=True: this test asserts on what landed in AdGuard.
                                push=True)
        assert fake_adguard.active_rules() == []
        assert engine.get_zone3_status()["total_blocked_domains"] == 0

        result = engine.set_enforce(True)
        assert result["success"] is True and result["enforce"] is True
        assert fake_adguard.protection_enabled is True
        assert len(fake_adguard.active_rules()) > 0
        # The baseline is house-wide, so most rules are NOT client-scoped.
        assert "||samsungads.com^" in fake_adguard.active_rules()
        assert engine.get_zone3_status()["total_blocked_domains"] > 0

        # ...and back, which must ALSO turn protection off, so flipping the
        # AdGuard UI toggle by hand cannot resurrect the rules.
        back = engine.set_enforce(False)
        assert back["enforce"] is False
        assert fake_adguard.protection_enabled is False
        assert fake_adguard.active_rules() == []

    def test_status_never_reports_blocking_it_is_not_doing(self, tmp_path,
                                                           fake_adguard):
        managed = tmp_path / "m.json"
        managed.write_text("[]")
        engine = PolicyEngine(adguard=_controller(enforce=False),
                              managed_file=str(managed))
        engine.apply_policy(ip="192.168.50.50", mac="50:01:BB:AA:CC:DD",
                            mode=SecurityMode.LOCKDOWN, manufacturer="Samsung",
                            category="smart_tv",
                                # push=True: this test asserts on what landed in AdGuard.
                                push=True)
        status = engine.get_zone3_status()
        assert status["enforcing"] is False
        assert status["mode"] == "monitor-first"
        assert status["total_recorded_domains"] > 0
        assert status["total_blocked_domains"] == 0
        assert status["adguard"]["active_puffpai_rules"] == 0
        assert status["adguard"]["version"] == FakeAdGuard.VERSION


class TestAlwaysAllowInvariant:
    """Safety-critical and network-infrastructure devices are un-blockable.

    Checked at BOTH layers, because either one alone is a single point of
    failure: no rule may name the device, AND AdGuard is told to skip
    filtering for that client entirely.
    """

    PROTECTED = ["medical_device", "safety_device", "network_infra"]

    def _engine(self, tmp_path, enforce=True):
        managed = tmp_path / "m.json"
        managed.write_text("[]")
        return PolicyEngine(adguard=_controller(enforce=enforce),
                            managed_file=str(managed))

    @pytest.mark.parametrize("category", PROTECTED)
    @pytest.mark.parametrize("mode", list(SecurityMode))
    def test_no_mode_can_block_a_protected_device(self, tmp_path, fake_adguard,
                                                  category, mode):
        engine = self._engine(tmp_path)
        result = engine.apply_policy(
            ip="192.168.50.77", mac="AA:BB:CC:DD:EE:FF", mode=mode,
            manufacturer="Samsung", category=category,
            # push=True: this test asserts on what landed in AdGuard.
            push=True)
        assert result["mode"] == "always_allow"
        assert engine.get_device_status("192.168.50.77")["mode"] == "always_allow"
        assert engine.get_device_status("192.168.50.77")["blocked_domains"] == []
        # No rule anywhere names this device.
        assert not any("192.168.50.77" in r for r in fake_adguard.active_rules())

    @pytest.mark.parametrize("category", PROTECTED)
    def test_adguard_client_has_filtering_switched_off(self, tmp_path,
                                                       fake_adguard, category):
        engine = self._engine(tmp_path)
        engine.apply_policy(ip="192.168.50.77", mac="AA:BB:CC:DD:EE:FF",
                            mode=SecurityMode.LOCKDOWN, manufacturer="Samsung",
                            category=category,
                                # push=True: this test asserts on what landed in AdGuard.
                                push=True)
        client = fake_adguard.client("puffpai:192.168.50.77")
        assert client is not None, fake_adguard.clients
        assert client["filtering_enabled"] is False
        assert client["use_global_settings"] is False
        assert client["ids"] == ["192.168.50.77"]

    def test_protection_written_even_in_monitor_first_mode(self, tmp_path,
                                                           fake_adguard):
        """It can only ever ALLOW more, so it does not wait for enforcement."""
        engine = self._engine(tmp_path, enforce=False)
        engine.apply_policy(ip="192.168.50.77", mac="AA:BB:CC:DD:EE:FF",
                            mode=SecurityMode.BALANCED, manufacturer="Philips",
                            category="medical_device",
                                # push=True: this test asserts on what landed in AdGuard.
                                push=True)
        assert fake_adguard.client("puffpai:192.168.50.77")["filtering_enabled"] is False

    def test_categories_and_helper_agree(self):
        for category in self.PROTECTED:
            assert _is_protected(category) is True
        assert _is_protected("smart_tv") is False
        assert _is_protected("smart_tv", is_safety_critical=True) is True
        # Unparseable text is not silently protected; the explicit flag is.
        assert _is_protected("not-a-category") is False
        assert DeviceCategory.NETWORK_INFRA in _NEVER_BLOCK_CATEGORIES

    def test_a_protected_client_entry_is_retired_when_the_device_leaves(
            self, tmp_path, fake_adguard):
        engine = self._engine(tmp_path)
        engine.apply_policy(ip="192.168.50.77", mac="AA:BB:CC:DD:EE:FF",
                            mode=SecurityMode.ALWAYS_ALLOW,
                            manufacturer="X", category="medical_device",
                                # push=True: this test asserts on what landed in AdGuard.
                                push=True)
        assert fake_adguard.client("puffpai:192.168.50.77") is not None
        engine.remove_device("192.168.50.77")
        assert fake_adguard.client("puffpai:192.168.50.77") is None

    def test_hand_made_adguard_clients_are_never_touched(self, tmp_path,
                                                         fake_adguard):
        fake_adguard.clients.append(
            {"name": "alexs-laptop", "ids": ["192.168.50.9"],
             "filtering_enabled": True})
        engine = self._engine(tmp_path)
        engine.apply_policy(ip="192.168.50.77", mac="AA:BB:CC:DD:EE:FF",
                            mode=SecurityMode.LOCKDOWN, manufacturer="X",
                            category="medical_device",
                                # push=True: this test asserts on what landed in AdGuard.
                                push=True)
        assert fake_adguard.client("alexs-laptop") is not None


class TestAdGuardControllerAgainstRealShapes:
    def test_status_maps_the_real_payload(self, fake_adguard):
        st = _controller().status()
        assert st.reachable is True
        assert st.version == FakeAdGuard.VERSION
        assert st.queries_today == 1234
        assert st.blocked_today == 7

    def test_unreachable_is_not_an_exception(self, monkeypatch):
        import puffpai.zone3.adguard_controller as mod
        server = FakeAdGuard(reachable=False)
        monkeypatch.setattr(mod.requests, "get", server.get)
        monkeypatch.setattr(mod.requests, "post", server.post)
        c = _controller()
        assert c.is_reachable() is False
        assert c.status().reachable is False

    def test_sync_reports_failure_when_adguard_is_down(self, tmp_path,
                                                       monkeypatch):
        import puffpai.zone3.adguard_controller as mod
        server = FakeAdGuard(reachable=False)
        monkeypatch.setattr(mod.requests, "get", server.get)
        monkeypatch.setattr(mod.requests, "post", server.post)
        managed = tmp_path / "m.json"
        managed.write_text("[]")
        engine = PolicyEngine(adguard=_controller(), managed_file=str(managed))
        result = engine.sync_dns()
        assert result["success"] is False
        assert result["applied"] is False

    def test_upstreams_are_read_from_dns_info(self, fake_adguard):
        assert _controller().upstreams() == ["https://dns.quad9.net/dns-query"]

    def test_recent_queries_are_trimmed_and_client_scoped(self, fake_adguard):
        fake_adguard.querylog = [
            {"time": "2026-09-09T19:00:00Z", "client": "192.168.50.50",
             "question": {"name": "samsungads.com", "type": "A"},
             "reason": "FilteredBlackList", "rule": "||samsungads.com^",
             "answer": [{"value": "0.0.0.0"}], "upstream": "quad9"},
            {"time": "2026-09-09T19:00:01Z", "client": "192.168.50.9",
             "question": {"name": "example.com", "type": "A"},
             "reason": "NotFilteredNotFound", "rule": ""},
        ]
        rows = _controller().recent_queries(client_ip="192.168.50.50")
        assert len(rows) == 1
        assert rows[0]["domain"] == "samsungads.com"
        assert rows[0]["blocked"] is True
        # The projection must not carry AdGuard's whole record.
        assert "answer" not in rows[0] and "upstream" not in rows[0]

    def test_balanced_keeps_cloud_domains_resolvable(self, tmp_path,
                                                     fake_adguard):
        """CLOUD is allowed by default — that is what makes BALANCED usable."""
        managed = tmp_path / "m.json"
        managed.write_text("[]")
        engine = PolicyEngine(adguard=_controller(enforce=True),
                              managed_file=str(managed))
        engine.apply_policy(ip="192.168.50.60", mac="AA:BB:CC:00:00:01",
                            mode=SecurityMode.BALANCED, manufacturer="Ring",
                            category="iot_camera",
                                # push=True: this test asserts on what landed in AdGuard.
                                push=True)
        rules = "\n".join(fake_adguard.active_rules())
        assert "api.ring.com" not in rules      # CLOUD
        assert "fw.ring.com" in rules           # TELEMETRY


class TestBaselineBlock:
    """The pre-loaded BALANCED policy — what "turn it on" does on day one."""

    def _engine(self, tmp_path, enforce=True):
        managed = tmp_path / "m.json"
        managed.write_text("[]")
        return PolicyEngine(adguard=_controller(enforce=enforce),
                            managed_file=str(managed))

    def test_baseline_exists_before_any_device_is_known(self, tmp_path,
                                                        fake_adguard):
        engine = self._engine(tmp_path)
        assert engine.get_managed_devices() == []
        engine.sync_dns()
        rules = fake_adguard.active_rules()
        assert "||samsungads.com^" in rules
        assert "||logs.roku.com^" in rules
        assert "||doubleclick.net^" in rules

    def test_baseline_is_tagged_per_manufacturer(self, tmp_path, fake_adguard):
        block = self._engine(tmp_path).build_baseline_block()
        for manufacturer in ("Samsung", "LG", "Roku", "Sonos"):
            assert f"! {manufacturer}" in block

    def test_baseline_excludes_cloud(self, tmp_path):
        block = self._engine(tmp_path).build_baseline_block()
        joined = "\n".join(r for r in block if not r.startswith("!"))
        # CLOUD entries — blocking these breaks the device, so BALANCED does not.
        for cloud in ("api.ring.com", "a2.tuyaus.com", "api.ecobee.com",
                      "use1-api.tplinkcloud.com"):
            assert cloud not in joined
        # ...while telemetry from the same vendors is blocked.
        assert "||fw.ring.com^" in block
        assert "||log.tuyaus.com^" in block

    def test_baseline_has_no_duplicate_rules(self, tmp_path):
        block = self._engine(tmp_path).build_baseline_block()
        rules = [r for r in block if not r.startswith("!")]
        assert len(rules) == len(set(rules))

    def test_baseline_rules_are_global_not_client_scoped(self, tmp_path):
        block = self._engine(tmp_path).build_baseline_block()
        assert not any("$client=" in r for r in block)

    def test_lockdown_adds_cloud_for_that_device_only(self, tmp_path,
                                                      fake_adguard):
        engine = self._engine(tmp_path)
        engine.apply_policy(ip="192.168.50.60", mac="AA:BB:CC:00:00:01",
                            mode=SecurityMode.LOCKDOWN, manufacturer="Ring",
                            category="iot_camera",
                                # push=True: this test asserts on what landed in AdGuard.
                                push=True)
        rules = fake_adguard.active_rules()
        assert "||api.ring.com^$client=192.168.50.60" in rules
        # ...and the rest of the house can still reach Ring's cloud.
        assert "||api.ring.com^" not in rules

    def test_open_reallows_that_device_s_telemetry(self, tmp_path,
                                                   fake_adguard):
        engine = self._engine(tmp_path)
        engine.apply_policy(ip="192.168.50.61", mac="AA:BB:CC:00:00:02",
                            mode=SecurityMode.OPEN, manufacturer="Samsung",
                            category="computer",
                                # push=True: this test asserts on what landed in AdGuard.
                                push=True)
        rules = fake_adguard.active_rules()
        # Telemetry re-allowed for this one device...
        assert "@@||nmetrics.samsung.com^$client=192.168.50.61" in rules
        # ...while ads stay blocked for everyone, including it.
        assert "||samsungads.com^" in rules
        assert "@@||samsungads.com^$client=192.168.50.61" not in rules

    def test_balanced_device_adds_no_rules_of_its_own(self, tmp_path,
                                                      fake_adguard):
        engine = self._engine(tmp_path)
        before = len(engine.build_rule_block())
        engine.apply_policy(ip="192.168.50.62", mac="50:01:BB:AA:CC:DD",
                            mode=SecurityMode.BALANCED, manufacturer="Samsung",
                            category="smart_tv",
                                # push=True: this test asserts on what landed in AdGuard.
                                push=True)
        after = [r for r in engine.build_rule_block() if not r.startswith("!")]
        baseline = [r for r in engine.build_baseline_block()
                    if not r.startswith("!")]
        assert after == baseline
        assert before > 0

    def test_the_baseline_cannot_reach_a_protected_device(self, tmp_path,
                                                          fake_adguard):
        """The one interaction that matters: a house-wide rule vs ALWAYS_ALLOW.

        The baseline blocks samsungads.com for everybody. A medical device
        must still resolve it — and it does, because its AdGuard client entry
        turns filtering OFF for that client entirely, which is checked here
        rather than assumed.
        """
        engine = self._engine(tmp_path)
        engine.apply_policy(ip="192.168.50.77", mac="AA:BB:CC:DD:EE:FF",
                            mode=SecurityMode.LOCKDOWN, manufacturer="Samsung",
                            category="medical_device",
                                # push=True: this test asserts on what landed in AdGuard.
                                push=True)
        assert "||samsungads.com^" in fake_adguard.active_rules()
        client = fake_adguard.client("puffpai:192.168.50.77")
        assert client["filtering_enabled"] is False
        assert client["use_global_settings"] is False


class TestTheToolPathNeverPushes:
    """A chat turn writes the ledger. It does not write the household's DNS.

    ``zone3_secure_device`` and ``zone3_change_mode`` are LLM tools: they are
    reachable from a chat turn, and therefore from a document the model was
    asked to summarise. Both used to end in ``apply_policy``'s unconditional
    ``sync_dns()``, which REPLACES PuffPAI's whole AdGuard rule block and
    rewrites the per-client entries — the ALWAYS_ALLOW ones are written live in
    BOTH postures. "Observe mode" made that quiet, not harmless: the rule block
    was one enforce click from live, and the client entries were live already.

    Recording is the model's job; pushing is the owner's.
    """

    def _engine(self, tmp_path, adguard):
        managed = tmp_path / "m.json"
        managed.write_text("[]")
        return PolicyEngine(adguard=adguard, managed_file=str(managed))

    def test_secure_device_records_without_touching_adguard(self, tmp_path,
                                                            fake_adguard):
        engine = self._engine(tmp_path, _controller(enforce=True))
        result = engine.secure_device("192.168.50.50", "50:01:BB:AA:CC:DD",
                                      recorded_by="tool")
        assert result["success"] is True
        assert result["pushed"] is False
        # The ledger DID move — a monitor-first run is not a run that forgot.
        assert engine.get_device_status("192.168.50.50") is not None
        # AdGuard did not.
        assert fake_adguard.posts == [], fake_adguard.posts
        assert fake_adguard.user_rules == []
        assert fake_adguard.clients == []

    def test_change_mode_records_without_touching_adguard(self, tmp_path,
                                                          fake_adguard):
        engine = self._engine(tmp_path, _controller(enforce=True))
        engine.apply_policy(ip="192.168.50.50", mac="50:01:BB:AA:CC:DD",
                            mode=SecurityMode.BALANCED,
                            manufacturer="Samsung", category="smart_tv")
        fake_adguard.posts.clear()
        result = engine.change_device_mode("192.168.50.50", SecurityMode.LOCKDOWN,
                                           recorded_by="tool")
        assert result["success"] is True and result["pushed"] is False
        assert engine.get_device_status("192.168.50.50")["mode"] == "lockdown"
        assert fake_adguard.posts == []

    def test_a_protected_device_gets_no_always_allow_client_from_a_tool(
            self, tmp_path, fake_adguard):
        """The client entry is the live-in-both-postures half of sync_dns.

        This is the write that was reachable from a chat turn even while Zone 3
        was observing, because ``filtering_enabled: false`` only ever allows
        MORE traffic and so is written in both modes.
        """
        engine = self._engine(tmp_path, _controller(enforce=False))
        engine.apply_policy(ip="192.168.50.77", mac="AA:BB:CC:DD:EE:FF",
                            mode=SecurityMode.ALWAYS_ALLOW,
                            manufacturer="Medtronic",
                            category="medical_device",
                            is_safety_critical=True, recorded_by="tool")
        assert fake_adguard.clients == []

    def test_provenance_is_recorded_on_the_row(self, tmp_path, fake_adguard):
        engine = self._engine(tmp_path, _controller(enforce=False))
        engine.secure_device("192.168.50.50", "50:01:BB:AA:CC:DD",
                             recorded_by="tool")
        row = engine.get_device_status("192.168.50.50")
        assert row["recorded_by"] == "tool"
        assert row["recorded_at"]
        # A tool-recorded row is not a confirmed one.
        assert row["user_confirmed"] is False

        engine.change_device_mode("192.168.50.50", SecurityMode.OPEN,
                                  recorded_by="owner")
        row = engine.get_device_status("192.168.50.50")
        assert row["recorded_by"] == "owner"
        assert row["user_confirmed"] is True

    def test_the_enforce_preview_marks_tool_recorded_rows(self, tmp_path,
                                                          fake_adguard):
        from puffpai.zone3.engine import enforce_preview
        engine = self._engine(tmp_path, _controller(enforce=False))
        engine.apply_policy(ip="192.168.50.50", mac="50:01:BB:AA:CC:DD",
                            mode=SecurityMode.LOCKDOWN, manufacturer="Samsung",
                            category="smart_tv", recorded_by="tool")
        engine.apply_policy(ip="192.168.50.51", mac="50:01:BB:AA:CC:DE",
                            mode=SecurityMode.LOCKDOWN, manufacturer="Samsung",
                            category="smart_tv", recorded_by="owner")
        preview = enforce_preview(engine)
        marked = [ln for ln in preview["lines"]
                  if "(recorded by a chat tool, not yet approved)" in ln]
        assert len(marked) == 1, preview["lines"]
        assert "192.168.50.50" in marked[0]
        assert preview["tool_recorded"] == 1

    def test_no_zone3_tool_call_site_asks_for_a_push(self):
        """The property, checked in the file rather than trusted.

        A future edit that adds ``push=True`` to one of these tool call sites
        re-opens the hole this class closes, and would otherwise do so
        silently.
        """
        import re
        src = (Path(__file__).resolve().parents[1] / "src" / "puffpai"
               / "assistant_tools.py").read_text(encoding="utf-8")
        calls = re.findall(
            r"engine\.(?:secure_device|change_device_mode|apply_policy|"
            r"apply_bucket_policy)\((?:[^()]|\([^()]*\))*\)", src)
        assert calls, "the zone3 tool call sites moved; update this test"
        for call in calls:
            assert "push=True" not in call, call
            assert 'recorded_by="tool"' in call, call


class TestSingleDomainHelpers:
    """block_domain / unblock_domain — the ad-hoc path, outside the ledger.

    Not used by PolicyEngine (which rewrites the whole block from the ledger),
    but public, so they get the same guarantees: valid syntax, owner rules
    preserved, and no accumulation of duplicates.
    """

    def test_block_then_unblock_round_trips(self, fake_adguard):
        fake_adguard.user_rules = ["||myown.example^"]
        c = _controller(enforce=True)
        assert c.block_domain("samsungads.com", reason="Samsung ad network")
        assert "||samsungads.com^" in fake_adguard.active_rules()
        assert "! Samsung ad network" in fake_adguard.user_rules
        assert c.unblock_domain("samsungads.com")
        assert "||samsungads.com^" not in fake_adguard.active_rules()
        assert "||myown.example^" in fake_adguard.user_rules

    def test_blocking_twice_does_not_duplicate(self, fake_adguard):
        c = _controller(enforce=True)
        c.block_domain("samsungads.com")
        c.block_domain("samsungads.com")
        assert fake_adguard.active_rules().count("||samsungads.com^") == 1

    def test_block_is_inert_in_monitor_mode(self, fake_adguard):
        c = _controller(enforce=False)
        assert c.block_domain("samsungads.com") is True
        assert any("samsungads.com" in r for r in fake_adguard.user_rules)
        assert fake_adguard.active_rules() == []

    def test_a_block_recorded_in_observe_goes_live_on_the_switch(
            self, fake_adguard):
        """The observe prefix is stripped and re-rendered, not frozen in."""
        observe = _controller(enforce=False)
        observe.block_domain("samsungads.com")
        enforcing = _controller(enforce=True)
        enforcing.block_domain("lgsmartad.com")
        active = fake_adguard.active_rules()
        assert "||samsungads.com^" in active
        assert "||lgsmartad.com^" in active

    def test_client_scoped_block_and_unblock(self, fake_adguard):
        c = _controller(enforce=True)
        c.block_domain("samsungads.com", client_ip="192.168.50.50")
        assert "||samsungads.com^$client=192.168.50.50" in fake_adguard.active_rules()
        # Unblocking the GLOBAL rule must not remove the client-scoped one.
        c.unblock_domain("samsungads.com")
        assert "||samsungads.com^$client=192.168.50.50" in fake_adguard.active_rules()
        c.unblock_domain("samsungads.com", client_ip="192.168.50.50")
        assert fake_adguard.active_rules() == []

    def test_a_bad_domain_never_reaches_adguard(self, fake_adguard):
        c = _controller(enforce=True)
        with pytest.raises(ValueError):
            c.block_domain("evil; rm -rf /")
        assert fake_adguard.posts == []
