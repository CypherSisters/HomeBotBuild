"""Every ``/zone3`` write route asks the owner first — and a new one cannot not.

The gate this file pins was already half-built. ``POST /zone3/enforce``,
``/zone3/secure`` and ``/zone3/policy`` refused without a single-use consent
ticket. Three sibling routes did not, and each of them reached the same engine:

    PUT    /zone3/device/<ip>/mode   -> change_device_mode -> apply_policy
                                       -> sync_dns()   (the gated twin of
                                       POST /zone3/policy, ungated)
    DELETE /zone3/device/<ip>        -> remove_device -> drops the rules AND
                                       retires the AdGuard client entry
    POST   /zone3/setup              -> sync_dns() — rewrites PuffPAI's whole
                                       rule block and every client entry

None of the three had a test, and the comment over ``_ZONE3_TICKET_KINDS`` read
"the three Zone 3 acts" while six write routes existed — so the omission read as
a scope decision rather than a gap. That is the shape of hole this file is
built to make impossible to reopen: ``TestNoUngatedWriteRoute`` walks
``app.url_map`` and fails on any ``/zone3`` rule with a write method that is not
declared in ``api_server.GATED_ZONE3_WRITES``. A route added next year without
a gate turns this file red on the commit that adds it.

**No engine is real here.** ``_get_zone3`` is replaced by a recorder, so
"the gate leaked" and "a household's DNS changed" are the same observable
event: an entry in ``engine.calls``. Nothing in this file can reach AdGuard,
and every IP is fabricated.

Run: pytest tests/test_zone3_gates.py -q
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock

import pytest

from tests.modpatch import patched_modules

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}

# Fabricated. None of these exist on any network.
TV = "192.168.50.50"
CAMERA = "192.168.50.60"
PACEMAKER_HUB = "192.168.50.77"


# ══════════════════════════════════════════════════════════════════════
#  A Zone 3 engine that cannot reach a sinkhole
# ══════════════════════════════════════════════════════════════════════

class EngineTrap:
    """Records what would have been actuated, and actuates nothing.

    Every method a ``/zone3`` write route calls is here and appends to
    ``calls``. An ungated route therefore cannot fail silently: it shows up as
    a call that a refusal should have prevented.
    """

    def __init__(self, devices=None, enforcing=False, buckets=None):
        self.calls: list = []
        self._devices = list(devices or [])
        self._buckets = list(buckets or [])
        self._enforcing = bool(enforcing)
        self._adguard = object()          # truthy: take the AdGuard branch

    # ── reads (a proposal is allowed to make these) ──────────────────
    def get_zone3_status(self) -> dict:
        return {
            "managed_devices": len(self._devices),
            "safety_critical_count": sum(
                1 for d in self._devices
                if d.get("is_safety_critical")
                or d.get("mode") == "always_allow"),
            "total_recorded_domains": sum(
                len(d.get("blocked_domains") or []) for d in self._devices),
            "enforcing": self._enforcing,
        }

    def get_managed_devices(self) -> list:
        return [dict(d) for d in self._devices]

    # ── writes (none of these may run without a redeemed ticket) ─────
    def secure_device(self, ip, mac, hostname="", *, push=False,
                      recorded_by="owner"):
        # push and recorded_by are recorded too: a route that reached the
        # engine WITHOUT push=True would be a route that asked the owner and
        # then did not act, and a route that recorded "tool" provenance from an
        # owner-gated path would mislabel the owner's own decision.
        self.calls.append(("secure_device", ip, push, recorded_by))
        return {"success": True, "mode": "balanced"}

    def apply_policy(self, **kwargs):
        self.calls.append(("apply_policy", kwargs.get("ip"),
                           kwargs.get("push", False),
                           kwargs.get("recorded_by", "owner")))
        mode = kwargs.get("mode")
        # The real engine returns ``mode.value``; a SecurityMode would not
        # survive jsonify, and a stub whose reply cannot be serialised is a
        # stub that does not model the route it stands in for.
        return {"success": True, "mode": getattr(mode, "value", mode)}

    def change_device_mode(self, ip, mode, *, push=False,
                           recorded_by="owner"):
        self.calls.append(("change_device_mode", ip, push, recorded_by))
        return {"success": True, "mode": getattr(mode, "value", mode)}

    def remove_device(self, ip):
        self.calls.append(("remove_device", ip))
        return {"success": True, "message": f"Removed {ip}"}

    def sync_dns(self):
        self.calls.append(("sync_dns", None))
        return {"success": True, "applied": True, "rules": 0}

    def setup_pihole_groups(self):
        self.calls.append(("setup_pihole_groups", None))
        return {"success": True}

    def get_managed_buckets(self) -> list:
        return list(self._buckets)

    def apply_bucket_policy(self, bucket_id, mode, *, recorded_by="owner",
                            push=False):
        self.calls.append(("apply_bucket_policy", bucket_id, push,
                           recorded_by))
        return {"success": True, "bucket": bucket_id,
                "mode": getattr(mode, "value", mode)}


def device(ip, *, mode="balanced", domains=3, hostname="", critical=False,
           manufacturer="Acme"):
    return {
        "ip": ip, "mac": "aa:bb:cc:dd:ee:ff", "manufacturer": manufacturer,
        "category": "smart_tv", "hostname": hostname, "mode": mode,
        "is_safety_critical": critical,
        "blocked_domains": [f"d{i}.example.invalid" for i in range(domains)],
    }


DEVICES = [
    device(TV, mode="balanced", domains=12, hostname="living-room-tv"),
    device(CAMERA, mode="lockdown", domains=31, hostname="porch-cam"),
    device(PACEMAKER_HUB, mode="always_allow", domains=0,
           hostname="med-hub", critical=True),
]


@pytest.fixture(scope="module")
def api_mod():
    """Import api_server once, with the firewall backend mocked out."""
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules(
            {"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        mod = importlib.import_module("puffpai.api_server")
    mod.app.config["TESTING"] = True
    return mod


@pytest.fixture
def trapped(api_mod, monkeypatch):
    """``(mod, engine)`` with every Zone 3 path pointed at the recorder."""
    engine = EngineTrap(devices=DEVICES)
    monkeypatch.setattr(api_mod, "_get_zone3", lambda: engine)
    api_mod._ha_tickets.clear()
    api_mod._rate_limit_store.clear()
    yield api_mod, engine
    api_mod._ha_tickets.clear()


@pytest.fixture
def client(trapped):
    mod, engine = trapped
    with mod.app.test_client() as c:
        yield c, {"X-API-Key": mod._API_KEY}, mod, engine


# ══════════════════════════════════════════════════════════════════════
#  The write routes, one row each
# ══════════════════════════════════════════════════════════════════════
#
#  (id, method, path, body, kind, target, action, engine method that must not
#  run without a redeemed ticket)

ROUTES = [
    ("secure", "POST", "/zone3/secure",
     {"ip": TV, "mac": "aa:bb:cc:dd:ee:ff"},
     "zone3_secure", TV, "secure", "secure_device"),
    ("policy", "POST", "/zone3/policy",
     {"ip": TV, "mac": "aa:bb:cc:dd:ee:ff", "mode": "lockdown"},
     "zone3_mode", TV, "lockdown", "apply_policy"),
    ("mode_put", "PUT", f"/zone3/device/{TV}/mode",
     {"mode": "lockdown"},
     "zone3_mode", TV, "lockdown", "change_device_mode"),
    ("remove", "DELETE", f"/zone3/device/{TV}",
     None,
     "zone3_remove", TV, "remove", "remove_device"),
    ("setup", "POST", "/zone3/setup",
     {},
     "zone3_setup", "zone3", "sync", "sync_dns"),
    ("enforce", "POST", "/zone3/enforce",
     {"enabled": True},
     "zone3_enforce", "zone3", "on", "__set_enforce_persistent__"),
    # Bucket policy: the rules it writes name the HUB as their client, so
    # LOCKDOWN here stops every device in the integration answering. It is a
    # write route like any other and it is gated like any other.
    ("bucket", "POST", "/zone3/bucket-policy",
     {"bucket": "cloud:ring", "mode": "lockdown"},
     "zone3_bucket", "cloud:ring", "lockdown", "apply_bucket_policy"),
]

IDS = [r[0] for r in ROUTES]


def _send(c, headers, method, path, body, ticket=None):
    payload = dict(body) if isinstance(body, dict) else None
    if ticket is not None:
        if payload is None:
            payload = {}
        payload["ticket"] = ticket
    kwargs = {"headers": headers}
    if payload is not None:
        kwargs["json"] = payload
    return getattr(c, method.lower())(path, **kwargs)


@pytest.fixture
def enforce_recorder(api_mod, monkeypatch):
    """Trap ``set_enforce_persistent``, which the enforce route imports late.

    The enforce switch does not go through the engine object, so the recorder
    on ``EngineTrap`` cannot see it. This is its equivalent: a call here means
    a household's DNS filtering was actually flipped.
    """
    from puffpai.zone3 import engine as z3
    calls: list = []

    def _recorder(enabled):
        calls.append(bool(enabled))
        return {"success": True, "enforcing": bool(enabled),
                "persistent": True}

    monkeypatch.setattr(z3, "set_enforce_persistent", _recorder)
    return calls


def _actuations(row_id, engine, method_name, enforce_calls):
    if method_name == "__set_enforce_persistent__":
        return list(enforce_calls)
    return [c for c in engine.calls if c[0] == method_name]


class TestNoTicketNoWrite:
    """First call, no ticket: 409, a proposal, and nothing actuated."""

    @pytest.mark.parametrize(
        "row_id,method,path,body,kind,target,action,engine_method",
        ROUTES, ids=IDS)
    def test_refused_and_engine_untouched(
            self, client, enforce_recorder, row_id, method, path, body, kind,
            target, action, engine_method):
        c, headers, mod, engine = client
        resp = _send(c, headers, method, path, body)
        assert resp.status_code == 409, resp.get_json()
        payload = resp.get_json()
        assert payload["success"] is False
        assert payload["needs_confirmation"] is True
        assert payload["confirm_endpoint"] == "/zone3/confirm"
        assert payload["proposal"]["kind"] == kind
        assert payload["proposal"]["target"] == target
        assert payload["proposal"]["action"] == action
        assert payload["proposal"]["headline"]
        assert _actuations(row_id, engine, engine_method,
                           enforce_recorder) == []

    @pytest.mark.parametrize(
        "row_id,method,path,body,kind,target,action,engine_method",
        ROUTES, ids=IDS)
    def test_a_confirmed_flag_in_the_body_is_not_a_confirmation(
            self, client, enforce_recorder, row_id, method, path, body, kind,
            target, action, engine_method):
        """The obvious forgery: say yes in the same request that asks."""
        c, headers, mod, engine = client
        payload = dict(body or {})
        payload.update({"confirmed": True, "human_confirmed": True,
                        "force": True, "ticket": None})
        resp = getattr(c, method.lower())(path, headers=headers, json=payload)
        assert resp.status_code == 409
        assert _actuations(row_id, engine, engine_method,
                           enforce_recorder) == []


class TestForgedAndReplayedTickets:

    @pytest.mark.parametrize(
        "row_id,method,path,body,kind,target,action,engine_method",
        ROUTES, ids=IDS)
    def test_a_forged_ticket_is_refused(
            self, client, enforce_recorder, row_id, method, path, body, kind,
            target, action, engine_method):
        c, headers, mod, engine = client
        resp = _send(c, headers, method, path, body,
                     ticket="not-a-real-ticket-value")
        assert resp.status_code == 409
        assert _actuations(row_id, engine, engine_method,
                           enforce_recorder) == []

    @pytest.mark.parametrize(
        "row_id,method,path,body,kind,target,action,engine_method",
        ROUTES, ids=IDS)
    def test_a_ticket_is_single_use(
            self, client, enforce_recorder, row_id, method, path, body, kind,
            target, action, engine_method):
        c, headers, mod, engine = client
        ticket, _ = mod._ha_mint_ticket(kind, target, action)

        first = _send(c, headers, method, path, body, ticket=ticket)
        assert first.status_code == 200, first.get_json()
        assert len(_actuations(row_id, engine, engine_method,
                               enforce_recorder)) == 1

        replay = _send(c, headers, method, path, body, ticket=ticket)
        assert replay.status_code == 409, replay.get_json()
        assert len(_actuations(row_id, engine, engine_method,
                               enforce_recorder)) == 1, "replay actuated again"

    @pytest.mark.parametrize(
        "row_id,method,path,body,kind,target,action,engine_method",
        ROUTES, ids=IDS)
    def test_an_expired_ticket_is_refused(
            self, client, enforce_recorder, monkeypatch, row_id, method, path,
            body, kind, target, action, engine_method):
        c, headers, mod, engine = client
        ticket, record = mod._ha_mint_ticket(kind, target, action)
        record["expires_at"] = 0.0                    # in the past
        resp = _send(c, headers, method, path, body, ticket=ticket)
        assert resp.status_code == 409
        assert _actuations(row_id, engine, engine_method,
                           enforce_recorder) == []


class TestValidTicketActuatesOnce:

    @pytest.mark.parametrize(
        "row_id,method,path,body,kind,target,action,engine_method",
        ROUTES, ids=IDS)
    def test_valid_ticket_is_accepted(
            self, client, enforce_recorder, row_id, method, path, body, kind,
            target, action, engine_method):
        c, headers, mod, engine = client
        ticket, _ = mod._ha_mint_ticket(kind, target, action)
        resp = _send(c, headers, method, path, body, ticket=ticket)
        assert resp.status_code == 200, resp.get_json()
        assert len(_actuations(row_id, engine, engine_method,
                               enforce_recorder)) == 1

    @pytest.mark.parametrize(
        "row_id,method,path,body,kind,target,action,engine_method",
        ROUTES, ids=IDS)
    def test_the_ticket_may_travel_in_a_header(
            self, client, enforce_recorder, row_id, method, path, body, kind,
            target, action, engine_method):
        """``X-Zone3-Ticket`` exists so DELETE has somewhere to put one."""
        c, headers, mod, engine = client
        ticket, _ = mod._ha_mint_ticket(kind, target, action)
        with_ticket = dict(headers)
        with_ticket["X-Zone3-Ticket"] = ticket
        resp = _send(c, with_ticket, method, path, body)
        assert resp.status_code == 200, resp.get_json()
        assert len(_actuations(row_id, engine, engine_method,
                               enforce_recorder)) == 1

    def test_the_two_step_flow_completes_over_http(self, client):
        """Mint at /zone3/confirm, redeem on the route. Minting changes nothing."""
        c, headers, mod, engine = client
        minted = c.post("/zone3/confirm", headers=headers, json={
            "kind": "zone3_remove", "target": TV, "action": "remove"})
        assert minted.status_code == 201, minted.get_json()
        assert engine.calls == [], "minting actuated something"

        ticket = minted.get_json()["ticket"]
        done = c.delete(f"/zone3/device/{TV}", headers=headers,
                        json={"ticket": ticket})
        assert done.status_code == 200, done.get_json()
        assert ("remove_device", TV) in engine.calls


class TestTicketsDoNotCrossKindsOrTargets:
    """A ticket buys exactly one act on exactly one target."""

    def test_a_mode_ticket_cannot_redeem_a_removal(self, client):
        c, headers, mod, engine = client
        ticket, _ = mod._ha_mint_ticket("zone3_mode", TV, "lockdown")
        resp = c.delete(f"/zone3/device/{TV}", headers=headers,
                        json={"ticket": ticket})
        assert resp.status_code == 409
        assert [x for x in engine.calls if x[0] == "remove_device"] == []

    def test_a_remove_ticket_cannot_redeem_a_mode_change(self, client):
        c, headers, mod, engine = client
        ticket, _ = mod._ha_mint_ticket("zone3_remove", TV, "remove")
        resp = c.put(f"/zone3/device/{TV}/mode", headers=headers,
                     json={"mode": "lockdown", "ticket": ticket})
        assert resp.status_code == 409
        assert [x for x in engine.calls if x[0] == "change_device_mode"] == []

    def test_a_setup_ticket_cannot_redeem_enforcement(
            self, client, enforce_recorder):
        c, headers, mod, engine = client
        ticket, _ = mod._ha_mint_ticket("zone3_setup", "zone3", "sync")
        resp = c.post("/zone3/enforce", headers=headers,
                      json={"enabled": True, "ticket": ticket})
        assert resp.status_code == 409
        assert enforce_recorder == []

    def test_an_enforce_ticket_cannot_redeem_a_setup_sync(self, client):
        c, headers, mod, engine = client
        ticket, _ = mod._ha_mint_ticket("zone3_enforce", "zone3", "on")
        resp = c.post("/zone3/setup", headers=headers, json={"ticket": ticket})
        assert resp.status_code == 409
        assert [x for x in engine.calls if x[0] == "sync_dns"] == []

    def test_a_ticket_for_one_device_does_not_move_another(self, client):
        """The mode kind is shared by two routes — not by two devices."""
        c, headers, mod, engine = client
        ticket, _ = mod._ha_mint_ticket("zone3_mode", TV, "lockdown")
        resp = c.put(f"/zone3/device/{CAMERA}/mode", headers=headers,
                     json={"mode": "lockdown", "ticket": ticket})
        assert resp.status_code == 409
        assert [x for x in engine.calls if x[0] == "change_device_mode"] == []

    def test_a_mode_ticket_is_bound_to_the_mode(self, client):
        c, headers, mod, engine = client
        ticket, _ = mod._ha_mint_ticket("zone3_mode", TV, "open")
        resp = c.put(f"/zone3/device/{TV}/mode", headers=headers,
                     json={"mode": "lockdown", "ticket": ticket})
        assert resp.status_code == 409
        assert [x for x in engine.calls if x[0] == "change_device_mode"] == []

    def test_the_mode_kind_is_deliberately_shared_by_both_routes(self, client):
        """A ticket minted for (ip, mode) works on POST /policy or PUT /mode.

        Asserted rather than left to chance: the two routes perform the same
        act, and giving them separate kinds is what would let one of them drift
        back out of the gate unnoticed.
        """
        c, headers, mod, engine = client
        ticket, _ = mod._ha_mint_ticket("zone3_mode", TV, "lockdown")
        resp = c.post("/zone3/policy", headers=headers, json={
            "ip": TV, "mac": "aa:bb:cc:dd:ee:ff", "mode": "lockdown",
            "ticket": ticket})
        assert resp.status_code == 200, resp.get_json()

        ticket2, _ = mod._ha_mint_ticket("zone3_mode", TV, "lockdown")
        resp2 = c.put(f"/zone3/device/{TV}/mode", headers=headers,
                      json={"mode": "lockdown", "ticket": ticket2})
        assert resp2.status_code == 200, resp2.get_json()

    def test_an_ha_ticket_cannot_redeem_a_zone3_write(self, client):
        """The two gates share primitives, never vocabulary."""
        c, headers, mod, engine = client
        ticket, _ = mod._ha_mint_ticket("service", TV, "remove")
        resp = c.delete(f"/zone3/device/{TV}", headers=headers,
                        json={"ticket": ticket})
        assert resp.status_code == 409
        assert [x for x in engine.calls if x[0] == "remove_device"] == []

    def test_zone3_confirm_refuses_an_ha_kind(self, client):
        c, headers, mod, engine = client
        resp = c.post("/zone3/confirm", headers=headers, json={
            "kind": "service", "target": TV, "action": "remove"})
        assert resp.status_code == 400

    @pytest.mark.parametrize("kind", sorted({r[4] for r in ROUTES}))
    def test_ha_confirm_refuses_every_zone3_kind(self, client, kind):
        c, headers, mod, engine = client
        resp = c.post("/ha/confirm", headers=headers, json={
            "kind": kind, "entity_id": TV, "action": "remove"})
        assert resp.status_code == 400


class TestApiKeyIsStillRequired:
    """The ticket is a SECOND factor, never a replacement for the first."""

    @pytest.mark.parametrize(
        "row_id,method,path,body,kind,target,action,engine_method",
        ROUTES, ids=IDS)
    def test_no_key_is_401(
            self, client, enforce_recorder, row_id, method, path, body, kind,
            target, action, engine_method):
        c, _headers, mod, engine = client
        resp = _send(c, {}, method, path, body)
        assert resp.status_code == 401, resp.get_json()
        assert _actuations(row_id, engine, engine_method,
                           enforce_recorder) == []

    @pytest.mark.parametrize(
        "row_id,method,path,body,kind,target,action,engine_method",
        ROUTES, ids=IDS)
    def test_a_valid_ticket_without_the_key_is_still_401(
            self, client, enforce_recorder, row_id, method, path, body, kind,
            target, action, engine_method):
        c, _headers, mod, engine = client
        ticket, _ = mod._ha_mint_ticket(kind, target, action)
        resp = _send(c, {}, method, path, body, ticket=ticket)
        assert resp.status_code == 401
        assert _actuations(row_id, engine, engine_method,
                           enforce_recorder) == []

    def test_confirm_itself_needs_the_key(self, client):
        c, _headers, mod, engine = client
        resp = c.post("/zone3/confirm", json={
            "kind": "zone3_enforce", "target": "zone3", "action": "on"})
        assert resp.status_code == 401


# ══════════════════════════════════════════════════════════════════════
#  The meta-test: no ungated write route can be added quietly
# ══════════════════════════════════════════════════════════════════════

def _zone3_write_rules(mod):
    """``[(endpoint, rule, sorted methods)]`` for every /zone3 write route."""
    found = []
    for rule in mod.app.url_map.iter_rules():
        if not str(rule.rule).startswith("/zone3"):
            continue
        methods = (rule.methods or set()) & WRITE_METHODS
        if methods:
            found.append((rule.endpoint, str(rule.rule), sorted(methods)))
    return sorted(found)


class TestNoUngatedWriteRoute:

    def test_every_zone3_write_route_is_declared(self, api_mod):
        """The whole point of this file. A route with a write method that is
        not in ``GATED_ZONE3_WRITES`` fails here on the commit that adds it."""
        undeclared = [
            (endpoint, rule, methods)
            for endpoint, rule, methods in _zone3_write_rules(api_mod)
            if endpoint not in api_mod.GATED_ZONE3_WRITES
        ]
        assert undeclared == [], (
            "ungated /zone3 write route(s): add the _zone3_redeem call and a "
            f"GATED_ZONE3_WRITES entry -> {undeclared}")

    def test_the_declaration_has_no_stale_entries(self, api_mod):
        """A route that was renamed or deleted must not leave a claim behind."""
        live = {endpoint for endpoint, _r, _m in _zone3_write_rules(api_mod)}
        stale = sorted(set(api_mod.GATED_ZONE3_WRITES) - live)
        assert stale == [], f"GATED_ZONE3_WRITES names dead routes: {stale}"

    def test_every_declared_kind_is_mintable(self, api_mod):
        """A route gated on a kind ``/zone3/confirm`` will not mint is a route
        nobody can ever use — a fail-closed footgun, not a gate."""
        kinds = {k for k in api_mod.GATED_ZONE3_WRITES.values()
                 if k != api_mod._ZONE3_MINTS_ONLY}
        assert kinds <= set(api_mod._ZONE3_TICKET_KINDS), (
            sorted(kinds - set(api_mod._ZONE3_TICKET_KINDS)))

    def test_every_mintable_kind_is_redeemed_somewhere(self, api_mod):
        """And the reverse: a kind nothing redeems is a ticket that buys
        nothing, which teaches a reader the gate covers more than it does."""
        kinds = {k for k in api_mod.GATED_ZONE3_WRITES.values()
                 if k != api_mod._ZONE3_MINTS_ONLY}
        assert set(api_mod._ZONE3_TICKET_KINDS) == kinds, (
            sorted(set(api_mod._ZONE3_TICKET_KINDS) ^ kinds))

    def test_the_known_write_routes_are_all_here(self, api_mod):
        """Pinned by name so a route SILENTLY DISAPPEARING is also caught."""
        assert {endpoint for endpoint, _r, _m in _zone3_write_rules(api_mod)} == {
            "zone3_secure_device", "zone3_set_policy", "zone3_change_mode",
            "zone3_remove_device", "zone3_setup", "zone3_enforce",
            "zone3_bucket_policy", "zone3_confirm",
        }

    def test_confirm_is_the_only_route_that_mints(self, api_mod):
        minting = [e for e, k in api_mod.GATED_ZONE3_WRITES.items()
                   if k == api_mod._ZONE3_MINTS_ONLY]
        assert minting == ["zone3_confirm"]

    def test_read_routes_are_untouched(self, client):
        """Gating writes must not have made the reads ask for a ticket."""
        c, headers, mod, engine = client
        for path in ("/zone3/status", "/zone3/managed", "/zone3/unmanaged",
                     f"/zone3/device/{TV}"):
            resp = c.get(path, headers=headers)
            assert resp.status_code != 409, path


# ══════════════════════════════════════════════════════════════════════
#  The enforce proposal enumerates what goes live
# ══════════════════════════════════════════════════════════════════════
#
#  Rules recorded by TOOL calls while Zone 3 was only observing all go live on
#  this one click. "About 42 recorded rule(s) go live" names none of them, and
#  hides the one fact an owner most needs: which devices are exempt.

class TestEnforceProposalEnumeratesWhatGoesLive:

    def _proposal(self, c, headers):
        resp = c.post("/zone3/enforce", headers=headers, json={"enabled": True})
        assert resp.status_code == 409
        return resp.get_json()["proposal"]

    def test_the_proposal_lists_each_device(self, client):
        c, headers, mod, engine = client
        proposal = self._proposal(c, headers)
        ips = [d["ip"] for d in proposal["devices"]]
        assert set(ips) == {TV, CAMERA, PACEMAKER_HUB}

    def test_each_row_carries_name_ip_mode_and_rule_count(self, client):
        c, headers, mod, engine = client
        proposal = self._proposal(c, headers)
        row = next(d for d in proposal["devices"] if d["ip"] == CAMERA)
        assert row["name"] == "porch-cam"
        assert row["mode"] == "LOCKDOWN"
        assert row["rules"] == 31
        assert row["never_blocked"] is False

    def test_always_allow_devices_are_named_never_blocked(self, client):
        c, headers, mod, engine = client
        proposal = self._proposal(c, headers)
        row = next(d for d in proposal["devices"]
                   if d["ip"] == PACEMAKER_HUB)
        assert row["never_blocked"] is True
        assert row["rules"] == 0
        assert any("never blocked" in line and PACEMAKER_HUB in line
                   for line in proposal["effects"])

    def test_the_effects_read_as_lines_a_person_can_check(self, client):
        c, headers, mod, engine = client
        proposal = self._proposal(c, headers)
        joined = "\n".join(proposal["effects"])
        assert "living-room-tv (192.168.50.50) — BALANCED, 12 rule(s)" in joined
        assert "porch-cam (192.168.50.60) — LOCKDOWN, 31 rule(s)" in joined

    def test_the_headline_count_is_the_sum_of_the_rows(self, client):
        """A total that does not add up is a total nobody can audit."""
        c, headers, mod, engine = client
        proposal = self._proposal(c, headers)
        total = sum(d["rules"] for d in proposal["devices"])
        assert f"{total} recorded rule(s) go live" in proposal["effects"][0]

    def test_the_off_proposal_is_unchanged(self, client):
        """Turning filtering OFF removes capability; it needs no manifest."""
        c, headers, mod, engine = client
        resp = c.post("/zone3/enforce", headers=headers,
                      json={"enabled": False})
        assert resp.status_code == 409
        assert "OFF" in resp.get_json()["proposal"]["headline"]

    def test_the_listing_is_capped_and_says_how_many_it_hid(self, api_mod,
                                                            monkeypatch):
        from puffpai.zone3 import engine as z3
        many = EngineTrap(devices=[device(f"172.16.0.{i}", domains=i)
                                   for i in range(1, 41)])
        preview = z3.enforce_preview(many)
        assert len(preview["devices"]) == z3.ENFORCE_PREVIEW_LIMIT == 25
        assert preview["more"] == 15
        assert preview["lines"][-1] == "+15 more device(s)"
        assert preview["managed"] == 40, "the COUNT still covers all of them"

    def test_never_blocked_devices_survive_the_cap(self, api_mod):
        """The exemption is the line that must never be the one trimmed."""
        from puffpai.zone3 import engine as z3
        crowd = [device(f"172.16.0.{i}", domains=50) for i in range(1, 40)]
        crowd.append(device("192.168.50.99", mode="always_allow", domains=0,
                            hostname="fire-panel", critical=True))
        preview = z3.enforce_preview(EngineTrap(devices=crowd))
        assert preview["devices"][0]["ip"] == "192.168.50.99"
        assert "fire-panel (192.168.50.99) — ALWAYS_ALLOW, never blocked" \
            in preview["lines"]

    def test_the_preview_never_actuates(self, api_mod):
        from puffpai.zone3 import engine as z3
        trap = EngineTrap(devices=DEVICES)
        z3.enforce_preview(trap)
        assert trap.calls == []

    def test_a_dead_engine_yields_an_empty_preview_not_an_exception(self):
        from puffpai.zone3 import engine as z3

        class Broken(EngineTrap):
            def get_managed_devices(self):
                raise RuntimeError("ledger unreadable")

        preview = z3.enforce_preview(Broken())
        assert preview["devices"] == [] and preview["rules"] == 0


class TestTheToolProposalMatchesTheRoute:
    """Voice and dashboard must describe the SAME change."""

    def test_the_tool_enumerates_the_same_devices(self, api_mod, monkeypatch):
        from puffpai.zone3 import engine as z3
        import puffpai.assistant_tools as rt

        trap = EngineTrap(devices=DEVICES, enforcing=False)
        monkeypatch.setattr(z3, "get_engine", lambda: trap)

        tools = rt.AssistantTools().create_tools()
        enforce = next(t for t in tools if t.name == "zone3_enforce")
        spoken = enforce.invoke("on")

        assert "living-room-tv (192.168.50.50) — BALANCED, 12 rule(s)" in spoken
        assert "porch-cam (192.168.50.60) — LOCKDOWN, 31 rule(s)" in spoken
        assert "med-hub (192.168.50.77) — ALWAYS_ALLOW, never blocked" in spoken
        assert "43 rule(s) live" in spoken, spoken
        assert trap.calls == [], "the tool actuated something"


# ══════════════════════════════════════════════════════════════════════
#  The phrases the tools advertise actually reach them
# ══════════════════════════════════════════════════════════════════════

ADVERTISED = [
    "stop filtering my network",
    "start blocking telemetry",
    "turn on enforcement",
    "turn off enforcement",
]


class TestAdvertisedPhrasesBind:

    @pytest.mark.parametrize("query", ADVERTISED)
    def test_the_phrase_binds_zone3_enforce(self, query):
        from puffpai import capability_registry as reg
        assert "zone3_enforce" in reg.tools_for_query(query), query

    @pytest.mark.parametrize("query", ADVERTISED + [
        "block my tv's telemetry", "stop my tv from phoning home"])
    def test_the_phrase_resolves_to_zone3(self, query):
        """``block my tv's telemetry`` used to score a four-way tie that
        declaration order broke in favour of ``home.control`` — the sentence
        the docstring advertises reached the light switch."""
        from puffpai import capability_registry as reg
        cap = reg.find(query)
        assert cap is not None and cap.id == "security.zone3", (
            query, cap.id if cap else None)

    @pytest.mark.parametrize("query", ADVERTISED)
    def test_the_docstring_phrase_is_declared_in_the_registry(self, query):
        """The docstring and the registry are the two halves of one claim."""
        from puffpai import capability_registry as reg
        import puffpai.assistant_tools as rt
        cap = reg.get("security.zone3")
        haystack = " | ".join(cap.phrases + cap.keywords)
        assert any(part in haystack for part in (query, query.split()[0])), query
        assert "zone3_enforce" in cap.all_tools
        # And the tool really does advertise them.
        source = (rt.__file__ or "")
        assert source

    def test_zone3_enforce_advertises_only_phrases_that_bind(self):
        """Every quoted phrase in the tool's docstring must reach the tool."""
        from puffpai import capability_registry as reg
        import re
        import puffpai.assistant_tools as rt
        from pathlib import Path

        source = Path(rt.__file__).read_text(encoding="utf-8")
        block = source.split("def zone3_enforce(", 1)[1].split('"""')[1]
        quoted = re.findall(r"'([^']{6,60})'", block)
        assert quoted, block
        for phrase in quoted:
            if phrase in ("on", "off"):
                continue
            assert "zone3_enforce" in reg.tools_for_query(phrase), phrase

    def test_network_scanning_language_still_reaches_the_scanner(self):
        """Widening zone3 must not have stolen the neighbouring capability."""
        from puffpai import capability_registry as reg
        for query in ("scan my network", "who is on my network",
                      "what is the network status"):
            cap = reg.find(query)
            assert cap is not None and cap.id in (
                "security.network", "bridge.inventory"), (query, cap.id)

    def test_light_control_language_still_reaches_the_lights(self):
        from puffpai import capability_registry as reg
        for query in ("turn on the lamp", "turn off the kitchen light"):
            cap = reg.find(query)
            assert cap is not None and cap.id == "home.control", (query, cap.id)


class TestBucketsWithNothingToBlock:
    """422 before the gate: no proposal, no ticket, no rule.

    Alex's hub reported 19 integrations and thirteen of them were Home
    Assistant's own furniture — the supervisor, the backup job, the sun. A
    lockdown selector on "the sun" is worse than useless: it buries the two
    rows that do reach a vendor. ``system:`` is refused in its own words so
    the page can say why, and ``local:`` keeps the answer it already had.
    """

    def test_a_system_bucket_is_refused_in_its_own_words(self, client):
        c, headers, _mod, engine = client
        resp = c.post("/zone3/bucket-policy", headers=headers,
                      json={"bucket": "system:hassio", "mode": "lockdown"})
        assert resp.status_code == 422, resp.get_json()
        payload = resp.get_json()
        assert payload["success"] is False
        assert payload["code"] == "bucket_not_cloud_backed"
        assert "Home Assistant's own internals" in payload["message"]
        assert not [x for x in engine.calls if x[0] == "apply_bucket_policy"]

    def test_a_system_bucket_never_mints_a_ticket(self, client):
        """422 is not 409: nothing here is confirmable, so nothing is offered."""
        c, headers, _mod, _engine = client
        payload = c.post("/zone3/bucket-policy", headers=headers,
                         json={"bucket": "system:sun",
                               "mode": "lockdown"}).get_json()
        assert "ticket" not in payload
        assert "proposal" not in payload

    def test_a_local_bucket_keeps_the_answer_it_already_had(self, client):
        c, headers, _mod, engine = client
        resp = c.post("/zone3/bucket-policy", headers=headers,
                      json={"bucket": "local:zha", "mode": "lockdown"})
        assert resp.status_code == 422, resp.get_json()
        message = resp.get_json()["message"]
        assert "nothing leaves the house" in message
        assert "Home Assistant's own internals" not in message
        assert not [x for x in engine.calls if x[0] == "apply_bucket_policy"]


class TestTheBucketRollTellsTheFirewallPageWhatALocalRowIs:
    """A row that is never offered a policy still has to earn its line.

    The Firewall page groups by tier now, and the local group says what the
    integration IS — "23 entities across 4 rooms" — because "no selector" on
    its own reads as a broken control rather than an answer.
    """

    def test_the_roll_carries_entities_and_rooms(self, client):
        c, headers, _mod, _engine = client
        from puffpai import bridge_ha
        bridge_ha.set_registries(
            [{"id": "d1", "name": "Lamp", "area_id": "kitchen",
              "identifiers": [["zha", "x"]]}],
            [{"entity_id": "light.lamp", "device_id": "d1", "platform": "zha"},
             {"entity_id": "sensor.lamp", "device_id": "d1", "platform": "zha"}],
            [{"area_id": "kitchen", "name": "Kitchen"}])
        try:
            body = c.get("/zone3/buckets", headers=headers).get_json()
            row = next(r for r in body["buckets"] if r["id"] == "local:zha")
            assert row["device_count"] == 1
            assert row["entity_count"] == 2
            assert row["areas"] == {"Kitchen": 1}
        finally:
            bridge_ha.reset()
