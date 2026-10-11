"""The Zigbee pairing window is an actuation, and it has to ask.

``ZigbeeManager.open_permit_join`` holds the Home Assistant token and POSTs
``services/zha/permit`` itself, so it does not pass ``HABridge.call_service``,
the choke point where the always-confirm gate lives. Pairing starts at
``puffpai.zigbee_pairing.request_pairing``, which files a consent request and
opens nothing; no LLM tool opens the window directly, and that pin lives with
the other legacy-tool pins in ``tests/test_ha_bridge_confirmation_gate.py``.

What an open window is, precisely: for its whole duration **any Zigbee device
within radio range may join the network**. The risk is physical proximity, not
an entity changing state, so no entry in ``SAFETY_CRITICAL_DOMAINS`` describes
it — which is why the gate here is applied with ``force=True`` rather than by
claiming ``zha`` is a critical domain.

Two properties these tests exist to keep, and they pull in opposite directions:

* **opening refuses** without a real :data:`HUMAN_CONFIRMED` token, and refuses
  *before any network call at all* — not before the POST, before everything;
* **closing never refuses**. ``zha.permit`` with ``duration=0`` is ZHA's stop
  path (there is no separate service), and a gate that can block the close is a
  gate that can hold a pairing window open.

**Nothing in this file may reach a real Home Assistant.** Every bridge here is
either a recorder or something that raises on contact, so "the gate failed" and
"the test called a real house" are the same event — and it surfaces as a failed
assertion about a list, not as a pairing window opening in someone's home. The
worked actuation is a fabricated coordinator; no real ieee appears anywhere.

Run: pytest tests/test_zigbee_permit_gate.py -q
"""

from __future__ import annotations

import json

import pytest

from puffpai.ha_bridge import (
    HUMAN_CONFIRMED,
    SAFETY_CRITICAL_DOMAINS,
    ConfirmationRequired,
    HABridge,
    HABridgeError,
)
from puffpai.zigbee_manager import (
    INSTALL_ACTION,
    PAIRING_WINDOW_TARGET,
    PERMIT_ACTION,
    PERMIT_DURATION_MAX,
    DetectedDevice,
    DongleSpec,
    ZigbeeManager,
    ZigbeeMatch,
    validate_ieee,
    validate_permit_duration,
)

# Fabricated. Not a real coordinator, not a real address.
FAKE_IEEE = "00:11:22:33:44:55:66:77"


# ══════════════════════════════════════════════════════════════════════
#  A bridge that cannot reach a real house
# ══════════════════════════════════════════════════════════════════════

class WireTrap(HABridge):
    """A real :class:`HABridge` with only ``_request`` — the one method that
    opens a socket — replaced by a recorder.

    Subclassing the real class is the point: the gate under test is the real
    ``call_service``, with the real argument handling, so a regression cannot
    hide behind a friendlier double.
    """

    def __init__(self):
        # Skip HABridge.__init__: it reads config and builds a Session.
        self.wire: list[tuple[str, str, object]] = []
        self._enabled = True
        self._url = "http://ha.invalid"

        class _NoCache:
            def get(self, key): return None
            def set(self, key, value): pass
            def invalidate(self, key=None): pass

        self._cache = _NoCache()

    def _request(self, endpoint, method="GET", data=None, timeout=None,
                 stream=False):
        self.wire.append((endpoint, method, data))

        class _Resp:
            def json(self): return []

        return _Resp()

    @property
    def service_calls(self):
        """Only the calls that would actuate — POSTs to ``services/…``."""
        return [(e, m, d) for e, m, d in self.wire
                if e.startswith("services/") and m == "POST"]


class Landmine:
    """Stands in for anything that must not be touched on a refused call.

    Attribute access explodes, so "the refusal came after a read" and "the
    refusal came after a direct REST call" both fail loudly instead of quietly
    costing a request against a real house.
    """

    def __init__(self, what: str):
        self._what = what

    def __getattr__(self, name):
        raise AssertionError(
            f"{self._what}.{name} was reached on a call that must have been "
            f"refused before touching anything")

    def __call__(self, *a, **k):
        raise AssertionError(f"{self._what} was called on a refused call")


@pytest.fixture
def mgr_and_wire(monkeypatch):
    """A manager wired to a recorder, with every other exit mined.

    ``is_zha_installed`` opens a WebSocket and ``_rest`` is the direct-POST
    session the fix removed; both are replaced with landmines so the "zero
    network calls" assertions cover more than the HTTP recorder.
    """
    monkeypatch.setattr(ZigbeeManager, "is_zha_installed",
                        lambda self: (_ for _ in ()).throw(AssertionError(
                            "is_zha_installed (a WebSocket read) ran on a call "
                            "that must have been refused first")))
    bridge = WireTrap()
    mgr = ZigbeeManager(url="http://ha.invalid", token="not-a-real-token",
                        bridge=bridge)
    mgr._rest = Landmine("_rest")
    return mgr, bridge


@pytest.fixture
def mgr_installed(monkeypatch):
    """A manager whose ZHA is 'installed', wired to a recorder.

    For the paths that are *allowed* to proceed. ``_rest`` is still mined: a
    permitted call must go through the bridge, not around it.
    """
    monkeypatch.setattr(ZigbeeManager, "is_zha_installed", lambda self: True)
    bridge = WireTrap()
    mgr = ZigbeeManager(url="http://ha.invalid", token="not-a-real-token",
                        bridge=bridge)
    mgr._rest = Landmine("_rest")
    return mgr, bridge


# ══════════════════════════════════════════════════════════════════════
#  Opening refuses, and refuses before touching anything
# ══════════════════════════════════════════════════════════════════════

def test_open_permit_join_unconfirmed_is_refused_with_zero_calls(mgr_and_wire):
    """The hole itself: opening a pairing window with nobody asked."""
    mgr, bridge = mgr_and_wire
    with pytest.raises(ConfirmationRequired):
        mgr.open_permit_join(duration=60)
    assert bridge.wire == [], (
        "a refused permit-join reached the network: " f"{bridge.wire}")


def test_refusal_carries_no_service_call_even_via_ieee(mgr_and_wire):
    """Naming a specific router does not route around the gate."""
    mgr, bridge = mgr_and_wire
    with pytest.raises(ConfirmationRequired):
        mgr.open_permit_join(duration=30, ieee=FAKE_IEEE)
    assert bridge.service_calls == []


@pytest.mark.parametrize("duration", [1, 60, PERMIT_DURATION_MAX])
def test_every_nonzero_duration_is_gated(mgr_and_wire, duration):
    """There is no window short enough to be free. One second is enough to
    accept a joiner, and the joiner stays joined."""
    mgr, bridge = mgr_and_wire
    with pytest.raises(ConfirmationRequired):
        mgr.open_permit_join(duration=duration)
    assert bridge.wire == []


def test_proposal_is_the_shape_the_409_handler_already_renders(mgr_and_wire):
    """No new envelope: ``_ha_error_response`` copies ``proposal`` verbatim, so
    it has to be the same ``{service, entity_id, domains}`` triple the dashboard
    already knows how to show and hand back to ``POST /ha/confirm``."""
    mgr, _ = mgr_and_wire
    with pytest.raises(ConfirmationRequired) as caught:
        mgr.open_permit_join(duration=60)

    proposal = caught.value.proposal
    assert set(proposal) == {"service", "entity_id", "domains"}
    assert proposal["service"] == PERMIT_ACTION == "zha.permit"
    assert proposal["entity_id"] == PAIRING_WINDOW_TARGET
    assert isinstance(proposal["domains"], list)
    # The whole body has to survive jsonify().
    json.dumps({"proposal": proposal, "error": str(caught.value)})


def test_proposal_target_is_redeemable_shaped(mgr_and_wire):
    """``POST /ha/confirm`` rejects a null or empty ``entity_id``, so a proposal
    carrying one could be rendered but never approved. ``zha.permit`` names no
    entity, hence the synthetic target."""
    mgr, _ = mgr_and_wire
    with pytest.raises(ConfirmationRequired) as caught:
        mgr.open_permit_join(duration=60)
    target = caught.value.proposal["entity_id"]
    assert isinstance(target, str) and target.strip()


def test_ieee_becomes_the_proposal_target(mgr_and_wire):
    """When a router is named it *is* the target — that is what moves where in
    the house a device can join from, so it is what a person should see."""
    mgr, _ = mgr_and_wire
    with pytest.raises(ConfirmationRequired) as caught:
        mgr.open_permit_join(duration=60, ieee=FAKE_IEEE)
    assert caught.value.proposal["entity_id"] == FAKE_IEEE


def test_refusal_says_what_the_risk_is(mgr_and_wire):
    """A refusal a household cannot read gets clicked through. The reason has to
    name radio range, not just 'confirmation required'."""
    mgr, _ = mgr_and_wire
    with pytest.raises(ConfirmationRequired) as caught:
        mgr.open_permit_join(duration=60)
    reason = str(caught.value).lower()
    assert "radio range" in reason
    assert "zha.permit" in reason


# ══════════════════════════════════════════════════════════════════════
#  Only the real token counts
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("spoof", [
    True, 1, "true", "yes", "HUMAN_CONFIRMED", "<HUMAN_CONFIRMED>",
    {"human_confirmed": True}, ["confirmed"], object(),
])
def test_a_value_is_not_a_confirmation(mgr_and_wire, spoof):
    """Everything decoded JSON can express is a *value*. The token is an object
    identity in this process, which ``json.loads`` has no syntax for — so a
    request body, or a model-written tool argument, cannot forge one."""
    mgr, bridge = mgr_and_wire
    with pytest.raises(ConfirmationRequired):
        mgr.open_permit_join(duration=60, human_confirmed=spoof)
    assert bridge.wire == []


def test_confirmed_open_reaches_the_wire_once(mgr_installed):
    mgr, bridge = mgr_installed
    result = mgr.open_permit_join(duration=45,
                                  human_confirmed=HUMAN_CONFIRMED)

    assert result["success"] is True
    assert result["closed"] is False
    assert bridge.service_calls == [
        ("services/zha/permit", "POST", {"duration": 45}),
    ]


def test_confirmed_open_with_ieee_forwards_a_normalised_address(mgr_installed):
    mgr, bridge = mgr_installed
    mgr.open_permit_join(duration=45, ieee=FAKE_IEEE.upper(),
                         human_confirmed=HUMAN_CONFIRMED)
    _, _, data = bridge.service_calls[0]
    assert data == {"duration": 45, "ieee": FAKE_IEEE}


def test_the_token_is_never_forwarded_to_home_assistant(mgr_installed):
    """``human_confirmed`` is consumed by ``call_service``'s keyword-only
    parameter. If it ever leaked into ``**service_data`` it would be POSTed to
    HA as service data — and would show up right here."""
    mgr, bridge = mgr_installed
    mgr.open_permit_join(duration=45, human_confirmed=HUMAN_CONFIRMED)
    _, _, data = bridge.service_calls[0]
    assert "human_confirmed" not in data
    assert "HUMAN_CONFIRMED" not in json.dumps(data)


def test_token_is_withheld_from_a_bridge_that_does_not_enforce(monkeypatch):
    """A double that does not declare ``ENFORCES_CONFIRMATION`` would receive
    the token in ``**kwargs`` and forward it to HA as service data. Same rule
    the Steward's proxy follows; withholding costs nothing because the verdict
    has already been reached."""
    monkeypatch.setattr(ZigbeeManager, "is_zha_installed", lambda self: True)

    class PlainDouble:
        def __init__(self): self.calls = []

        def call_service(self, domain, service, entity_id=None, **kwargs):
            self.calls.append((domain, service, entity_id, kwargs))
            return []

    double = PlainDouble()
    mgr = ZigbeeManager(url="http://ha.invalid", token="not-a-real-token",
                        bridge=double)
    mgr._rest = Landmine("_rest")
    mgr.open_permit_join(duration=30, human_confirmed=HUMAN_CONFIRMED)

    _, _, _, kwargs = double.calls[0]
    assert "human_confirmed" not in kwargs


# ══════════════════════════════════════════════════════════════════════
#  Closing is never gated
# ══════════════════════════════════════════════════════════════════════

def test_close_permit_join_needs_no_confirmation(mgr_installed):
    """Reducing exposure is always allowed. A gate that can refuse this is a
    gate that can hold a pairing window open."""
    mgr, bridge = mgr_installed
    result = mgr.close_permit_join()

    assert result["success"] is True
    assert result["closed"] is True
    assert bridge.service_calls == [
        ("services/zha/permit", "POST", {"duration": 0}),
    ]


def test_duration_zero_is_the_close_path(mgr_installed):
    """``duration=0`` is ZHA's stop — there is no separate close service — so it
    has to be exempt however it is spelled."""
    mgr, bridge = mgr_installed
    mgr.open_permit_join(duration=0)
    assert bridge.service_calls[0][2] == {"duration": 0}


def test_close_through_a_named_router_is_not_gated(mgr_installed):
    mgr, bridge = mgr_installed
    result = mgr.close_permit_join(ieee=FAKE_IEEE)
    assert result["success"] is True
    assert bridge.service_calls[0][2] == {"duration": 0, "ieee": FAKE_IEEE}


def test_string_zero_also_closes_rather_than_being_refused(mgr_installed):
    """The LLM tool passes a string. ``"0"`` must not fall through the coercion
    into the gated branch — that would refuse a close."""
    mgr, bridge = mgr_installed
    mgr.open_permit_join(duration="0")
    assert bridge.service_calls[0][2] == {"duration": 0}


# ══════════════════════════════════════════════════════════════════════
#  Malformed arguments die before any request
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad", [
    "sixty", "", None, 3.5, True, False, -1, -0.0001, 255, 10 ** 9,
    "60; drop", "0x10", "6 0", [], {}, ["60"],
])
def test_malformed_duration_is_refused_before_any_request(mgr_and_wire, bad):
    """Rejected at the door, not coerced. ``int(3.9)`` silently truncates and
    ``bool`` is an ``int`` subclass, so ``True`` would otherwise have become a
    one-second window; ``255`` is outside ZHA's own 0–254 schema."""
    mgr, bridge = mgr_and_wire
    with pytest.raises(HABridgeError) as caught:
        mgr.open_permit_join(duration=bad)
    assert str(caught.value).startswith("invalid "), (
        "message must start with 'invalid ' so the API renders it 400, not 502")
    assert bridge.wire == []


@pytest.mark.parametrize("bad", [
    "not-an-ieee", "aa:bb", "zz:bb:cc:dd:ee:ff:00:11", 12345,
    "aa:bb:cc:dd:ee:ff:00:11:22", ["aa"], {"ieee": FAKE_IEEE},
])
def test_malformed_ieee_is_refused_before_any_request(mgr_and_wire, bad):
    mgr, bridge = mgr_and_wire
    with pytest.raises(HABridgeError):
        mgr.open_permit_join(duration=60, ieee=bad)
    assert bridge.wire == []


def test_malformed_argument_is_a_400_not_a_409(mgr_and_wire):
    """Validation before policy: a caller who sent garbage gets told it is
    garbage, rather than being offered a confirmation for a call that could
    never have been made."""
    mgr, _ = mgr_and_wire
    with pytest.raises(HABridgeError) as caught:
        mgr.open_permit_join(duration="sixty")
    assert not isinstance(caught.value, ConfirmationRequired)


def test_validators_accept_the_legitimate_range():
    assert validate_permit_duration(0) == 0
    assert validate_permit_duration("60") == 60
    assert validate_permit_duration(PERMIT_DURATION_MAX) == PERMIT_DURATION_MAX
    assert validate_ieee(None) is None
    assert validate_ieee("  ") is None
    assert validate_ieee(FAKE_IEEE.upper()) == FAKE_IEEE


# ══════════════════════════════════════════════════════════════════════
#  The other bypass in this file: the config-entry flow
# ══════════════════════════════════════════════════════════════════════

FAKE_SPEC = DongleSpec("10c4", "ea60", "Fake Test Dongle", "znp")
FAKE_DEVICE = DetectedDevice(
    vid="10c4", pid="ea60", serial_number="TEST0000",
    manufacturer="Test", description="fabricated test dongle",
    device_path="/dev/ttyTEST0",
)
FAKE_MATCH = ZigbeeMatch(FAKE_DEVICE, FAKE_SPEC, "low")


def test_install_zha_unconfirmed_is_refused_before_any_request(monkeypatch):
    """The second bypass. ``install_zha`` POSTs the config-entry flow directly,
    which claims a USB serial device and forms a network. A ``low`` confidence
    match — the CP2102 VID/PID is shared with adapters that are not Zigbee
    coordinators — can take over something else entirely."""
    monkeypatch.setattr(ZigbeeManager, "is_zha_installed", Landmine("is_zha_installed"))
    monkeypatch.setattr(ZigbeeManager, "_flow_post", Landmine("_flow_post"))
    mgr = ZigbeeManager(url="http://ha.invalid", token="not-a-real-token",
                        bridge=WireTrap())
    mgr._rest = Landmine("_rest")

    with pytest.raises(ConfirmationRequired) as caught:
        mgr.install_zha(FAKE_MATCH)

    proposal = caught.value.proposal
    assert set(proposal) == {"service", "entity_id", "domains"}
    assert proposal["service"] == INSTALL_ACTION
    assert proposal["entity_id"] == "/dev/ttyTEST0"


def test_install_zha_refusal_names_the_port_and_the_confidence(monkeypatch):
    monkeypatch.setattr(ZigbeeManager, "is_zha_installed", Landmine("is_zha_installed"))
    monkeypatch.setattr(ZigbeeManager, "_flow_post", Landmine("_flow_post"))
    mgr = ZigbeeManager(url="http://ha.invalid", token="not-a-real-token")
    mgr._rest = Landmine("_rest")

    with pytest.raises(ConfirmationRequired) as caught:
        mgr.install_zha(FAKE_MATCH)
    reason = str(caught.value)
    assert "/dev/ttyTEST0" in reason
    assert "low" in reason


def test_install_zha_with_confirmation_reaches_the_flow(monkeypatch):
    monkeypatch.setattr(ZigbeeManager, "is_zha_installed", lambda self: False)
    posted: list[tuple] = []

    def fake_flow_post(self, flow_id, body):
        posted.append((flow_id, body))
        # Aborts immediately: the stub proves the gate let the first POST
        # through, and never lets a config entry be created even in a test.
        return {"type": "abort", "flow_id": "test-flow-id",
                "reason": "test_stub_never_creates_an_entry"}

    monkeypatch.setattr(ZigbeeManager, "_flow_post", fake_flow_post)
    mgr = ZigbeeManager(url="http://ha.invalid", token="not-a-real-token")

    result = mgr.install_zha(FAKE_MATCH, human_confirmed=HUMAN_CONFIRMED)

    assert posted and posted[0] == (None, {"handler": "zha"})
    assert result["success"] is False and result["step"] == "abort"


@pytest.mark.parametrize("spoof", [True, "true", 1])
def test_install_zha_rejects_a_forged_confirmation(monkeypatch, spoof):
    monkeypatch.setattr(ZigbeeManager, "is_zha_installed", Landmine("is_zha_installed"))
    monkeypatch.setattr(ZigbeeManager, "_flow_post", Landmine("_flow_post"))
    mgr = ZigbeeManager(url="http://ha.invalid", token="not-a-real-token")
    mgr._rest = Landmine("_rest")

    with pytest.raises(ConfirmationRequired):
        mgr.install_zha(FAKE_MATCH, human_confirmed=spoof)


def test_abort_flow_is_not_gated(monkeypatch):
    """Same exemption as closing the window: cancelling a half-made install can
    only undo. Requiring approval to clean up would leave dangling flows behind
    every refusal."""
    deleted: list[str] = []

    class _RestRecorder:
        def delete(self, url, timeout=None):
            deleted.append(url)

    mgr = ZigbeeManager(url="http://ha.invalid", token="not-a-real-token")
    mgr._rest = _RestRecorder()
    mgr._abort_flow("test-flow-id")

    assert deleted == [
        "http://ha.invalid/api/config/config_entries/flow/test-flow-id"]


# ══════════════════════════════════════════════════════════════════════
#  Regression pins on the shape of the fix
# ══════════════════════════════════════════════════════════════════════

def test_no_direct_service_url_is_built_in_this_module():
    """The bypass was a hand-built ``/api/services/zha/permit`` URL. Anything
    that reconstructs one is going around ``call_service`` again, so pin the
    absence of the pattern rather than only the behaviour of today's callers."""
    import inspect

    import puffpai.zigbee_manager as module
    source = inspect.getsource(module)
    assert "/api/services/" not in source
    assert "api/services" not in source.replace("call_service", "")


def test_permit_actuation_goes_through_the_bridge_not_the_local_session(
        mgr_installed):
    """``_rest`` is a landmine here, so a permitted call that still used the
    module's own session would explode instead of quietly working."""
    mgr, bridge = mgr_installed
    mgr.open_permit_join(duration=30, human_confirmed=HUMAN_CONFIRMED)
    assert len(bridge.service_calls) == 1


def test_zha_is_not_claimed_to_be_a_safety_critical_domain():
    """The gate is deliberately not implemented by adding ``zha`` to
    ``SAFETY_CRITICAL_DOMAINS``: that set is domain-keyed, and it would gate the
    *close* path too. If someone adds it, this test says so and the close-path
    tests above are the reason it matters."""
    assert "zha" not in SAFETY_CRITICAL_DOMAINS


def test_human_confirmed_is_keyword_only_everywhere():
    """A positional confirmation parameter can be filled by a caller splatting
    external arguments. Both entry points keep it keyword-only."""
    import inspect

    for func in (ZigbeeManager.open_permit_join, ZigbeeManager.install_zha):
        param = inspect.signature(func).parameters["human_confirmed"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, func.__name__
