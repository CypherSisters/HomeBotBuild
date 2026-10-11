""""Turn on enforcement" must reach Zone 3, not the light switch.

**"turn on enforcement"** must not be swallowed by the
Home Assistant ``turn on <X>`` path and answered with a device
disambiguation list, the way **"start enforcing at the network edge"**, the
same act phrased without the words a light switch owns, reaches
``zone3_enforce`` and returns a proposal. The registry already BINDS these
phrases to ``security.zone3``; binding is not dispatch, so the sentence still
has to survive routing to get there.

The fix is a ``_try_*`` fastpath registered in ``_try_instant_answer`` — the
house pattern for a misrouted intent (never ``_detect_route``). This file
pins three things:

  1. Each of the seven enforcement phrasings answers with a PROPOSAL.
  2. No engine write happens on any of them. ``set_enforce``, ``sync_dns``
     and ``change_device_mode`` are on a trap that records and actuates
     nothing, so "the fastpath actuated" and "a household's DNS changed" are
     the same observable event.
  3. Three real Home Assistant sentences are NOT claimed by it.

Plus the registry-level shadowing check: no declared zone3 / bridge phrase
may resolve to ``home.control``. That tie is what put "start blocking
telemetry" on the light switch in the first place.

**No engine is real here.** Nothing in this file can reach AdGuard or DNS.

Run: pytest tests/test_zone3_routing.py -q
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import sys

import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tests.modpatch import patched_modules  # noqa: E402


# ══════════════════════════════════════════════════════════════════════
#  A Zone 3 engine that cannot reach a sinkhole
# ══════════════════════════════════════════════════════════════════════

class EngineTrap:
    """Records what would have been actuated, and actuates nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_enforce(self, *a, **kw):
        self.calls.append(("set_enforce", a, kw))
        return {"success": True}

    def sync_dns(self, *a, **kw):
        self.calls.append(("sync_dns", a, kw))
        return {"success": True}

    def change_device_mode(self, *a, **kw):
        self.calls.append(("change_device_mode", a, kw))
        return {"success": True}

    def secure_device(self, *a, **kw):
        self.calls.append(("secure_device", a, kw))
        return {"success": True}

    def remove_device(self, *a, **kw):
        self.calls.append(("remove_device", a, kw))
        return {"success": True}

    def get_zone3_status(self):
        return {"managed_devices": 4, "safety_critical_count": 1,
                "total_recorded_domains": 12, "enforcing": False}

    def discover_devices(self):
        return []

    def get_unmanaged_devices(self):
        return []


_PREVIEW = {
    "managed": 4, "critical": 1, "rules": 12, "more": 0,
    "lines": ["the tv — 6 domain(s)", "the doorbell — 6 domain(s)"],
    "devices": [{"name": "the tv", "rules": 6},
                {"name": "the doorbell", "rules": 6}],
}


@contextlib.contextmanager
def _legacy_import_path():
    """Make src/puffpai importable flat, for create_tools' pre-package imports."""
    flat = str(pathlib.Path(__file__).resolve().parents[1] / "src" / "puffpai")
    sys.path.insert(0, flat)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(flat)


@pytest.fixture(scope="module")
def api():
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules(
            {"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        import importlib
        mod = importlib.import_module("puffpai.api_server")
        assert hasattr(mod, "_try_zone3_enforce"), "stale api_server on sys.path"
        yield mod


class _Tools:
    """The real tool bodies, reachable the way api_server reaches them."""

    def __init__(self, tool_dict):
        self.tool_dict = tool_dict

    def run(self, message, history):        # the legacy agent path
        return "LEGACY-AGENT"

    def daily_briefing(self):
        return "LEGACY-BRIEFING"


@pytest.fixture
def zone3(api, monkeypatch):
    """Real zone3 tools bound to the trap; the rest of chat stubbed out."""
    from puffpai.assistant_tools import AssistantTools
    from puffpai.zone3 import engine as z3engine

    trap = EngineTrap()
    state = {"enforcing": False}

    monkeypatch.setattr(z3engine, "get_engine", lambda *a, **kw: trap)
    monkeypatch.setattr(z3engine, "resolve_enforce",
                        lambda *a, **kw: state["enforcing"])
    monkeypatch.setattr(z3engine, "enforce_source", lambda *a, **kw: "the test")
    monkeypatch.setattr(z3engine, "enforce_preview",
                        lambda *a, **kw: dict(_PREVIEW))

    stand_in = type("_Stand", (), {"_ha_bridge": MagicMock()})()
    with _legacy_import_path():
        made = AssistantTools.create_tools(stand_in)
    tools = _Tools({t.name: t for t in made})

    monkeypatch.setattr(api, "_get_assistant_tools", lambda: tools)
    monkeypatch.setattr(api, "_snapshot_counts", lambda msg: {})
    monkeypatch.setattr(api, "_verify_and_fix", lambda msg, resp, before: resp)
    monkeypatch.setattr(api, "_load_persona", lambda: "persona")
    monkeypatch.setattr(api, "_try_direct_handle", lambda msg: None)
    monkeypatch.setattr(
        api, "_query_ollama_direct",
        lambda message, history, persona, enriched=False, **kw: {
            "response": "LEGACY-RAG", "sources": [], "suggestions": []})
    monkeypatch.setattr(api, "_route_via_concierge",
                        lambda text, requester="owner", **kw: None)

    trap.state = state
    return trap


# ══════════════════════════════════════════════════════════════════════
#  1 + 2 — the seven enforcement phrasings answer with a proposal, and
#          nothing is actuated
# ══════════════════════════════════════════════════════════════════════

# (phrase, the direction it asks for). The posture is set to the OPPOSITE of
# what is asked, because zone3_enforce answers "Zone 3 is already there" when
# the switch is where you asked for it — which is correct and is not a
# proposal.
ENFORCEMENT_PHRASES = [
    ("turn on enforcement", "on"),
    ("turn enforcement off", "off"),
    ("start blocking telemetry", "on"),
    ("stop filtering my network", "off"),
    ("block my tv's telemetry", "on"),
    ("enable zone 3 enforcement", "on"),
    ("start enforcing at the network edge", "on"),
]

WRITES = {"set_enforce", "sync_dns", "change_device_mode"}


@pytest.mark.parametrize("phrase,want", ENFORCEMENT_PHRASES)
def test_enforcement_phrasing_answers_with_a_proposal(api, zone3, phrase, want):
    zone3.state["enforcing"] = (want == "off")

    out = api._process_chat_query(phrase)
    reply = out["response"]

    assert "Proposal" in reply, reply
    assert [c[0] for c in zone3.calls if c[0] in WRITES] == [], zone3.calls


@pytest.mark.parametrize("phrase,want", ENFORCEMENT_PHRASES)
def test_enforcement_phrasing_is_claimed_by_the_fastpath(api, zone3, phrase,
                                                         want):
    """Same seven, at the helper — so a failure names the layer that broke."""
    zone3.state["enforcing"] = (want == "off")
    assert "Proposal" in (api._try_zone3_enforce(phrase) or "")


# ══════════════════════════════════════════════════════════════════════
#  3 — the light switch keeps its own sentences
# ══════════════════════════════════════════════════════════════════════

HA_PHRASES = [
    "turn on the kitchen lights",
    "turn on the porch light",
    "turn off the tv",
]


@pytest.mark.parametrize("phrase", HA_PHRASES)
def test_home_assistant_phrasing_is_not_claimed(api, zone3, phrase):
    assert api._try_zone3_enforce(phrase) is None
    assert zone3.calls == []


@pytest.mark.parametrize("phrase", HA_PHRASES)
def test_home_assistant_phrasing_never_reaches_zone3_in_chat(api, zone3,
                                                             phrase):
    out = api._process_chat_query(phrase)
    assert "Proposal" not in out["response"], out["response"]
    assert [c[0] for c in zone3.calls if c[0] in WRITES] == []


def test_a_status_question_is_left_to_zone3_status(api, zone3):
    """Naming the subject is not asking for a change."""
    assert api._try_zone3_enforce("is my network being filtered") is None


# ══════════════════════════════════════════════════════════════════════
#  The headline must not read as done
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("phrase,want", [(p, w) for p, w in ENFORCEMENT_PHRASES])
def test_proposal_headline_does_not_claim_the_change_happened(api, zone3,
                                                              phrase, want):
    """Live chat led with "✅ Proposal created: … turned ON" and only THEN
    said it needed approval. The first thing the owner reads decides what she
    believes happened."""
    zone3.state["enforcing"] = (want == "off")
    reply = api._try_zone3_enforce(phrase)

    assert "approval" in reply, reply
    head = reply[:reply.index("approval")]
    for done in ("turned ON", "turned OFF", "✅"):
        assert done not in head, f"{done!r} before 'approval' in: {head!r}"


# ══════════════════════════════════════════════════════════════════════
#  The registry: no declared phrase may resolve to the light switch
# ══════════════════════════════════════════════════════════════════════

def _declared_phrases(cap_id):
    from puffpai import capability_registry as reg
    cap = reg.get(cap_id) if hasattr(reg, "get") else None
    if cap is None:
        cap = next(c for c in reg.all_capabilities()
                   if getattr(c, "id", getattr(c, "capability_id", "")) == cap_id)
    phrases = tuple(getattr(cap, "phrases", ()) or ())
    phrases += tuple(getattr(cap, "dispatch_phrases", ()) or ())
    assert phrases, f"{cap_id} declares no phrases"
    return cap, phrases


def _cap_id(cap):
    return getattr(cap, "id", getattr(cap, "capability_id", ""))


@pytest.mark.parametrize("cap_id", ["security.zone3", "bridge.inventory"])
def test_no_declared_phrase_falls_through_to_home_control(cap_id):
    """The shadowing check proper: not one declared phrase may land on the
    light switch, and not one may bind nothing at all. ``home.control`` won
    "start blocking telemetry" on declaration order alone, which is how an
    advertised sentence reached a lamp."""
    from puffpai import capability_registry as reg
    _cap, phrases = _declared_phrases(cap_id)
    bad = [(p, _cap_id(reg.find(p)) if reg.find(p) else None)
           for p in phrases
           if reg.find(p) is None or _cap_id(reg.find(p)) == "home.control"]
    assert bad == [], f"{cap_id} phrases shadowed by home.control: {bad}"


def test_every_zone3_phrase_resolves_to_zone3():
    """Stricter, for the capability this file is about: each declared zone3
    phrase must bind security.zone3 exactly — not a neighbour."""
    from puffpai import capability_registry as reg
    _cap, phrases = _declared_phrases("security.zone3")
    misses = [(p, _cap_id(reg.find(p)) if reg.find(p) else None)
              for p in phrases
              if reg.find(p) is None or _cap_id(reg.find(p)) != "security.zone3"]
    assert misses == [], misses


def test_bridge_inventory_phrases_that_bind_a_neighbour_are_pinned():
    """Two bridge.inventory phrases bind a SIBLING, not the light switch, and
    both readings are arguable — "list my devices" is also the Steward's HA
    roll (the registry says so in a comment beside the keywords), and "what is
    on my network" is also the network-status read. Pinned rather than
    silently fixed: changing which capability owns them is a product call, not
    a routing bug, and this test turns red the day the answer changes.

    "what is on my network" was ONE of those two until the managed-device view
    shipped. That was the product call: with Home Assistant's ~100 devices now
    bucketed onto the Bridge roll, the device roll is the fuller answer to that
    sentence than a router scan is, so the phrase moved and the pin shrank to
    one entry."""
    from puffpai import capability_registry as reg
    _cap, phrases = _declared_phrases("bridge.inventory")
    elsewhere = {p: _cap_id(reg.find(p)) for p in phrases
                 if reg.find(p) is not None
                 and _cap_id(reg.find(p)) != "bridge.inventory"}
    assert elsewhere == {"list my devices": "home.devices"}
