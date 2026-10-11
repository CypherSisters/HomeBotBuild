"""`POST /system/egress/policy` — the owner switch for `chat_allow_egress`.

`chat_allow_egress` decides whether an ad-hoc chat turn may carry an egress
budget at all. Without this route it can only be set in
`config/puffpai_config.json` or the unit file, both of which a hardened box
(`ProtectSystem=strict`, config outside `ReadWritePaths`) makes unwritable by
the service: a setting with no switch, where the owner's only route to their
own opt-in is an ssh session and a root editor.

It resolves through the same four tiers a door does
(`env > owner overlay > config > default-off`) with its own overlay file, and
this route writes the overlay tier. Same auth, same rate-limit bucket, same
refusal shapes as the door route, deliberately — it is the same class of act.

The properties pinned here, hardest first:

* **Auth, including from loopback.** A 200 without a key is a critical hole.
* **It grants nothing on its own.** Turning the opt-in on with every web door
  shut must leave chat egress refused. This is the one that keeps the opt-in
  from becoming a master switch.
* **The value is never coerced**, an env override is a refusal rather than an
  inert write, and an unparseable overlay is never rebuilt.
* **The door overlay is untouched** by a policy write, and vice versa.

No real credential appears here; the API key is the synthetic one conftest
seeds.
"""

import importlib
import json
import os
import sys
from pathlib import Path
from typing import NamedTuple

import pytest
from unittest.mock import MagicMock

from tests.modpatch import patched_modules

_src_dir = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.security import egress_doors, egress_ledger, egress_policy  # noqa: E402


@pytest.fixture(scope="module")
def api_app():
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules({"firewall_manager":
                          MagicMock(FirewallManager=lambda: mock_fw)}):
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")
        mod.app.config["TESTING"] = True
        yield mod.app, mod._API_KEY, mod


@pytest.fixture()
def client(api_app):
    app, api_key, mod = api_app
    mod._rate_limit_store.clear()
    with app.test_client() as c:
        yield c, {"X-API-Key": api_key}, mod


class Sandbox(NamedTuple):
    """The files a policy write could touch, so a test can name the one it means."""
    cfg: "Path"
    ledger: "Path"
    policy: "Path"
    door_overlay: "Path"


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    cfg = tmp_path / "puffpai_config.json"
    cfg.write_text(json.dumps({"identity": {"name": "TestBot"}}, indent=2),
                   encoding="utf-8")
    ledger = tmp_path / "egress.jsonl"
    policy = tmp_path / "state" / "egress_policy.json"
    door_overlay = tmp_path / "state" / "egress_doors.json"

    monkeypatch.setattr(egress_doors, "_config_path", lambda: str(cfg))
    egress_doors.set_overlay_override(str(door_overlay))
    egress_policy.set_policy_override(str(policy))
    egress_policy.reset_cache()
    egress_ledger.set_path_override(str(ledger))
    monkeypatch.setattr(egress_doors, "_ledger_path",
                        lambda: str(tmp_path / "snapshot.json"))
    try:
        yield Sandbox(cfg, ledger, policy, door_overlay)
    finally:
        egress_ledger.set_path_override(None)
        egress_policy.set_policy_override(None)
        egress_doors.set_overlay_override(None)
        egress_policy.reset_cache()


def ledger_lines(path) -> list:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in
            path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ── auth ─────────────────────────────────────────────────────────────────────

class TestAuthentication:
    def test_no_key_is_401(self, client, sandbox):
        c, _headers, _mod = client
        resp = c.post("/system/egress/policy",
                      json={"id": "chat_allow_egress", "value": True})
        assert resp.status_code == 401

    def test_loopback_does_not_substitute_for_the_key(self, client, sandbox):
        """`localhost` IS the peer here. A 200 would be a critical hole."""
        c, _headers, _mod = client
        resp = c.post("/system/egress/policy",
                      json={"id": "chat_allow_egress", "value": True},
                      environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        assert resp.status_code == 401
        assert not sandbox.policy.exists()

    def test_the_route_is_not_a_public_endpoint(self, api_app):
        _app, _key, mod = api_app
        assert "/system/egress/policy" not in mod._PUBLIC_ENDPOINTS

    def test_the_route_is_not_loopback_exempt(self):
        from puffpai.security import endpoint_gate
        assert "/system/egress/policy" not in getattr(
            endpoint_gate, "LOCAL_ONLY_ENDPOINTS", set())


# ── refusals ─────────────────────────────────────────────────────────────────

class TestRefusals:
    @pytest.mark.parametrize("body", [
        {"value": True},
        {"id": "chat_allow_egress"},
        {"id": "chat_allow_egress", "value": "true"},
        {"id": "chat_allow_egress", "value": 1},
        {"id": "", "value": True},
        [],
    ])
    def test_malformed_bodies_are_400(self, client, sandbox, body):
        """`value` is never coerced, for the same reason `open` is not."""
        c, headers, _mod = client
        resp = c.post("/system/egress/policy", headers=headers, json=body)
        assert resp.status_code == 400
        assert not sandbox.policy.exists()

    def test_unknown_policy_is_404(self, client, sandbox):
        c, headers, _mod = client
        resp = c.post("/system/egress/policy", headers=headers,
                      json={"id": "no_such_policy", "value": True})
        assert resp.status_code == 404

    def test_an_env_override_is_refused_rather_than_written_uselessly(
            self, client, sandbox, monkeypatch):
        monkeypatch.setenv("PUFFPAI_CHAT_ALLOW_EGRESS", "0")
        egress_policy.reset_cache()
        c, headers, _mod = client
        resp = c.post("/system/egress/policy", headers=headers,
                      json={"id": "chat_allow_egress", "value": True})
        assert resp.status_code == 409
        assert "PUFFPAI_CHAT_ALLOW_EGRESS" in resp.get_json()["error"]
        assert not sandbox.policy.exists()

    def test_an_unparseable_overlay_is_refused_and_never_rebuilt(
            self, client, sandbox):
        """Rebuilding would discard every other setting the owner had made."""
        sandbox.policy.parent.mkdir(parents=True, exist_ok=True)
        sandbox.policy.write_text("{not json", encoding="utf-8")
        before = sandbox.policy.read_bytes()

        c, headers, _mod = client
        resp = c.post("/system/egress/policy", headers=headers,
                      json={"id": "chat_allow_egress", "value": True})
        assert resp.status_code == 409
        assert sandbox.policy.read_bytes() == before

    def test_a_refusal_writes_no_ledger_line(self, client, sandbox):
        c, headers, _mod = client
        c.post("/system/egress/policy", headers=headers,
               json={"id": "no_such_policy", "value": True})
        assert [r for r in ledger_lines(sandbox.ledger)
                if r.get("event") == "policy_toggled"] == []


# ── the round trip ───────────────────────────────────────────────────────────

class TestRoundTrip:
    def test_a_toggle_lands_in_the_overlay_and_reads_back(self, client,
                                                          sandbox):
        c, headers, _mod = client
        resp = c.post("/system/egress/policy", headers=headers,
                      json={"id": "chat_allow_egress", "value": True})
        assert resp.status_code == 200
        row = resp.get_json()["policy"]
        assert row["enabled"] is True
        assert row["source"] == "overlay"

        # A SEPARATE read, i.e. the cache really was reset.
        assert egress_policy.enabled("chat_allow_egress") is True

        stored = json.loads(sandbox.policy.read_text(encoding="utf-8"))
        assert stored["kind"] == egress_policy.POLICY_KIND
        assert stored["policies"] == {"chat_allow_egress": True}

    def test_turning_it_off_again_reads_back_off(self, client, sandbox):
        c, headers, _mod = client
        c.post("/system/egress/policy", headers=headers,
               json={"id": "chat_allow_egress", "value": True})
        resp = c.post("/system/egress/policy", headers=headers,
                      json={"id": "chat_allow_egress", "value": False})
        assert resp.status_code == 200
        assert resp.get_json()["policy"]["enabled"] is False
        assert egress_policy.enabled("chat_allow_egress") is False

    def test_the_appliance_config_is_never_touched(self, client, sandbox):
        """No config-file fallback exists for policies, and none is created."""
        before = sandbox.cfg.read_bytes()
        c, headers, _mod = client
        c.post("/system/egress/policy", headers=headers,
               json={"id": "chat_allow_egress", "value": True})
        assert sandbox.cfg.read_bytes() == before

    def test_a_policy_write_does_not_touch_the_door_overlay(self, client,
                                                            sandbox):
        """Separate files so one write can never change the other's meaning."""
        c, headers, _mod = client
        c.post("/system/egress/policy", headers=headers,
               json={"id": "chat_allow_egress", "value": True})
        assert not sandbox.door_overlay.exists()

    def test_a_door_write_does_not_touch_the_policy_overlay(self, client,
                                                            sandbox):
        c, headers, _mod = client
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": "web_fetch", "open": True})
        assert resp.status_code == 200
        assert not sandbox.policy.exists()

    def test_no_temp_file_is_left_behind(self, client, sandbox):
        c, headers, _mod = client
        c.post("/system/egress/policy", headers=headers,
               json={"id": "chat_allow_egress", "value": True})
        leftovers = [p.name for p in sandbox.policy.parent.iterdir()
                     if "tmp" in p.name]
        assert leftovers == []

    def test_the_toggle_is_audited(self, client, sandbox):
        c, headers, _mod = client
        c.post("/system/egress/policy", headers=headers,
               json={"id": "chat_allow_egress", "value": True})
        rows = [r for r in ledger_lines(sandbox.ledger)
                if r.get("event") == "policy_toggled"]
        assert len(rows) == 1
        assert rows[0]["door"] == "chat_allow_egress"
        assert rows[0]["open"] is True
        assert rows[0]["by"] == "owner-api"
        assert rows[0]["stored"] == "overlay"

    def test_the_audit_line_survives_the_public_projection(self, client,
                                                           sandbox):
        """The Settings card's recent-decisions list must show the toggle."""
        c, headers, _mod = client
        c.post("/system/egress/policy", headers=headers,
               json={"id": "chat_allow_egress", "value": True})
        resp = c.get("/system/egress/ledger?limit=10", headers=headers)
        assert resp.status_code == 200
        records = resp.get_json()["records"]
        toggles = [r for r in records if r.get("event") == "policy_toggled"]
        assert toggles, records
        for record in records:
            for leaky in ("host", "path", "url", "query", "caller"):
                assert leaky not in record


# ── the property that keeps this from being a master switch ──────────────────

class TestGrantsNothingAlone:
    def test_the_opt_in_alone_does_not_grant_chat_egress(self, client,
                                                         sandbox):
        """Every web door shut: the opt-in must change nothing.

        This is the whole reason the opt-in and the doors are ANDed. An owner
        who closed the web doors has said what they want; a chat opt-in that
        reopened them would be the appliance overriding that.
        """
        c, headers, _mod = client
        c.post("/system/egress/policy", headers=headers,
               json={"id": "chat_allow_egress", "value": True})

        allowed, why = egress_policy.chat_egress_allowed()
        assert allowed is False
        assert "no web door is open" in why

    def test_both_together_do_grant_it(self, client, sandbox):
        c, headers, _mod = client
        c.post("/system/egress/policy", headers=headers,
               json={"id": "chat_allow_egress", "value": True})
        c.post("/system/egress/door", headers=headers,
               json={"id": "web_fetch", "open": True})

        assert egress_policy.chat_egress_allowed()[0] is True

    def test_the_chat_budget_follows_the_same_answer(self, client, sandbox,
                                                     monkeypatch):
        """`_chat_envelope_budget` must not have its own opinion."""
        _c, _headers, mod = client
        monkeypatch.setattr(
            "puffpai.agents.planning_loop.planning_loop_enabled",
            lambda: True)

        assert mod._chat_envelope_budget().allow_egress is False

        _c.post("/system/egress/policy", headers=_headers,
                json={"id": "chat_allow_egress", "value": True})
        _c.post("/system/egress/door", headers=_headers,
                json={"id": "web_fetch", "open": True})
        assert mod._chat_envelope_budget().allow_egress is True


# ── status publication ───────────────────────────────────────────────────────

class TestStatus:
    def test_every_policy_publishes_a_valid_source(self, sandbox):
        for row in egress_policy.policy_status():
            assert row["source"] in egress_policy.POLICY_SOURCES

    def test_an_env_forced_policy_is_not_writable(self, sandbox, monkeypatch):
        monkeypatch.setenv("PUFFPAI_CHAT_ALLOW_EGRESS", "1")
        egress_policy.reset_cache()
        row = [r for r in egress_policy.policy_status()
               if r["id"] == "chat_allow_egress"][0]
        assert row["enabled"] is True
        assert row["source"] == "env"
        assert row["writable"] is False

    def test_writable_agrees_with_what_the_route_accepts(self, client,
                                                          sandbox):
        c, headers, _mod = client
        for row in egress_policy.policy_status():
            resp = c.post("/system/egress/policy", headers=headers,
                          json={"id": row["id"], "value": True})
            if row["writable"]:
                assert resp.status_code == 200, row["id"]
            else:
                assert resp.status_code == 409, row["id"]

    def test_a_junk_overlay_value_is_not_consent(self, sandbox):
        sandbox.policy.parent.mkdir(parents=True, exist_ok=True)
        sandbox.policy.write_text(json.dumps({
            "kind": egress_policy.POLICY_KIND,
            "policies": {"chat_allow_egress": "maybe"},
        }), encoding="utf-8")
        egress_policy.reset_cache()
        value, source, reason = egress_policy.resolve("chat_allow_egress")
        assert value is False
        assert source == "overlay"
        assert "not a flag value" in reason

    def test_an_unparseable_overlay_leaves_every_policy_off(self, sandbox):
        sandbox.policy.parent.mkdir(parents=True, exist_ok=True)
        sandbox.policy.write_text("{{{", encoding="utf-8")
        egress_policy.reset_cache()
        assert egress_policy.enabled("chat_allow_egress") is False

    def test_the_config_tier_is_still_honoured(self, sandbox):
        """An appliance that already set the key keeps its setting."""
        sandbox.cfg.write_text(json.dumps({
            "agents": {"chat_allow_egress": True}}), encoding="utf-8")
        egress_policy.reset_cache()
        value, source, _reason = egress_policy.resolve("chat_allow_egress")
        assert value is True
        assert source == "config"

    def test_the_overlay_outranks_the_config(self, sandbox):
        sandbox.cfg.write_text(json.dumps({
            "agents": {"chat_allow_egress": True}}), encoding="utf-8")
        sandbox.policy.parent.mkdir(parents=True, exist_ok=True)
        sandbox.policy.write_text(json.dumps({
            "kind": egress_policy.POLICY_KIND,
            "policies": {"chat_allow_egress": False},
        }), encoding="utf-8")
        egress_policy.reset_cache()
        value, source, _reason = egress_policy.resolve("chat_allow_egress")
        assert value is False
        assert source == "overlay"

    def test_the_env_outranks_the_overlay(self, sandbox, monkeypatch):
        """An operator's unit-file lockdown is not undoable by the service."""
        sandbox.policy.parent.mkdir(parents=True, exist_ok=True)
        sandbox.policy.write_text(json.dumps({
            "kind": egress_policy.POLICY_KIND,
            "policies": {"chat_allow_egress": True},
        }), encoding="utf-8")
        monkeypatch.setenv("PUFFPAI_CHAT_ALLOW_EGRESS", "0")
        egress_policy.reset_cache()
        value, source, _reason = egress_policy.resolve("chat_allow_egress")
        assert value is False
        assert source == "env"
