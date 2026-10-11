"""The six required "answer from real data" phrasings.

Three of them are easy to get wrong:

* "whats the status of the house" must reach a capability read. Sent to
  ``[Chat] Route: rag`` instead, it can come back as a fully invented answer
  ("living-room lights are on, the thermostat is set to 72F, no water-leak
  sensors have tripped") sourced from unrelated ZIM articles.
* "is the vault locked" / "vault status" must reach the vault's lock state.
  ``vault.secrets -> vault_list`` lists labels and cannot answer a lock-state
  question by construction.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import capability_registry as reg  # noqa: E402
from puffpai.agents import household_reads  # noqa: E402

REQUIRED_PHRASINGS = [
    "what can you do",
    "what is the network status",
    "whats in the pantry",
    "is the vault locked",
    "vault status",
    "whats the status of the house",
]


# ── the house status read (never RAG, never invented) ────────────────────────

class _FakeBridge:
    """States shaped exactly like ``HomeAssistantBridge.get_all_states``."""

    def __init__(self, states):
        self._states = states

    def get_all_states(self, force=False):
        return self._states


def _state(entity_id, state, **attrs):
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


HOUSE = [
    _state("light.kitchen", "on", friendly_name="Kitchen"),
    _state("light.hallway", "off", friendly_name="Hallway"),
    _state("light.porch", "off", friendly_name="Porch"),
    _state("climate.bedroom", "cool", friendly_name="Bedroom",
           temperature=72, current_temperature=74),
    _state("binary_sensor.under_sink", "off", friendly_name="Under Sink",
           device_class="moisture"),
    _state("binary_sensor.front_door", "on", friendly_name="Front Door",
           device_class="door"),
    _state("sensor.some_power_meter", "412", friendly_name="Power"),
    _state("automation.sunset_lights", "on", friendly_name="Sunset lights"),
]


def test_house_status_counts_the_real_lights():
    answer = household_reads.house_status(bridge=_FakeBridge(HOUSE))
    assert "1 of 3 lights are on" in answer


def test_house_status_reports_the_real_thermostat():
    answer = household_reads.house_status(bridge=_FakeBridge(HOUSE))
    assert "Bedroom" in answer and "72" in answer and "74" in answer


def test_house_status_reports_safety_sensors_and_open_doors():
    answer = household_reads.house_status(bridge=_FakeBridge(HOUSE))
    assert "no leak, smoke or gas sensor has tripped" in answer
    assert "Front Door" in answer


def test_house_status_raises_the_alarm_when_a_leak_sensor_is_wet():
    wet = list(HOUSE)
    wet[4] = _state("binary_sensor.under_sink", "on",
                    friendly_name="Under Sink", device_class="moisture")
    answer = household_reads.house_status(bridge=_FakeBridge(wet))
    assert "ALERT" in answer and "Under Sink" in answer


def test_house_status_does_not_dump_the_device_inventory():
    """The other half of the failure: a 111-entity dump is not an answer."""
    answer = household_reads.house_status(bridge=_FakeBridge(HOUSE))
    assert "automation.sunset_lights" not in answer
    assert "sensor.some_power_meter" not in answer
    assert len(answer) < 600


def test_house_status_says_so_when_home_assistant_is_unreachable():
    """It must never describe a house it cannot see. This is the fabrication."""

    class _Down:
        def get_all_states(self, force=False):
            raise RuntimeError("connection refused")

    answer = household_reads.house_status(bridge=_Down())
    assert "can't reach Home Assistant" in answer
    assert "72" not in answer and "leak" not in answer.split("no live reading")[0].lower()


@pytest.mark.parametrize("text", [
    "whats the status of the house",
    "what's the status of the house",
    "what is the status of the house",
    "house status",
    "how is the house",
    "hows the house doing",
])
def test_house_phrasings_reach_the_house_reader(text):
    assert household_reads.match(text) == "house_status"


@pytest.mark.parametrize("text", [
    "tell me about the white house",
    "what is a house plant",
    "who wrote house of leaves",
    "play some house music",
])
def test_house_reader_declines_the_other_houses(text):
    assert household_reads.match(text) is None


# ── vault lock state (not vault_list) ────────────────────────────────────────

class _FakeVault:
    def __init__(self, initialized=True, unlocked=False, entries=()):
        self.is_initialized = initialized
        self.has_live_session = unlocked
        self._vault_dir = _FakeDir(entries)


class _FakeFile:
    def __init__(self, name):
        self.name = name

    def is_file(self):
        return True


class _FakeDir:
    def __init__(self, names):
        self._names = list(names)

    def exists(self):
        return True

    def iterdir(self):
        return [_FakeFile(n) for n in self._names]


def test_locked_vault_says_locked_and_counts_without_naming():
    answer = household_reads.vault_status(
        _FakeVault(entries=("router_password.json", "bank_pin.json",
                            ".vault_key_salt", "auto_detected_pii.json",
                            "doc_passport.json")))
    assert "LOCKED" in answer
    assert "2 protected items" in answer
    # The labels ARE the sensitive part; a status answer must not volunteer them.
    assert "router" not in answer.lower() and "bank" not in answer.lower()


def test_unlocked_vault_says_unlocked():
    answer = household_reads.vault_status(
        _FakeVault(unlocked=True, entries=("router_password.json",)))
    assert "UNLOCKED" in answer
    assert "1 protected item" in answer


def test_uninitialized_vault_says_it_is_not_set_up():
    answer = household_reads.vault_status(_FakeVault(initialized=False))
    assert "isn't set up yet" in answer
    assert "LOCKED" not in answer


def test_empty_but_initialized_vault_still_answers_the_lock_question():
    """The old answer — 'set up but empty' — never said locked or unlocked."""
    answer = household_reads.vault_status(_FakeVault(entries=()))
    assert "LOCKED" in answer
    assert "no items stored yet" in answer


# ── the registry declares it, and declares it on the RIGHT capability ────────

@pytest.mark.parametrize("text", [
    "is the vault locked", "is my vault locked", "vault status",
    "what is my vault status", "whats my vault status",
    "is the vault unlocked", "status of my vault",
])
def test_lock_state_phrasings_dispatch_to_vault_status(text):
    cap = reg.find_for_dispatch(text)
    assert cap is not None, f"{text!r} falls through"
    assert cap.id == "vault.status"
    assert cap.authority_tool == "vault_status"


def test_vault_secrets_no_longer_claims_the_lock_question():
    secrets = reg.get("vault.secrets")
    for phrase in secrets.dispatch_phrases:
        assert "lock" not in phrase, (
            f"{phrase!r} still dispatches a lock question to vault_list")
        assert "status" not in phrase


def test_contents_phrasings_still_reach_vault_secrets():
    """The split must not cost vault.secrets the question it does answer."""
    for text in ("what's in my vault", "list my vault", "my vault contents"):
        assert reg.find_for_dispatch(text).id == "vault.secrets"


def test_vault_status_capability_is_read_only():
    assert reg.get("vault.status").access == reg.READ


# ── "what can you do" comes from the registry ───────────────────────────────

def test_what_can_you_do_is_registry_backed():
    described = reg.describe_for("what can you do")
    assert described
    domains = {c.domain for c in reg.all_capabilities()}
    named = sum(1 for d in domains if d in described.lower())
    assert named >= 5, "the overview must be built from the registry's domains"


def test_every_required_phrasing_has_a_declared_owner():
    """None of the six may be left to RAG's best guess."""
    import puffpai.api_server as api

    owners = {}
    for text in REQUIRED_PHRASINGS:
        if api._CAPABILITIES_RE.search(text):
            owners[text] = "introspection"
            continue
        cap = reg.find_for_dispatch(text)
        if cap is not None:
            owners[text] = cap.id
            continue
        for tool_name, match_re, exclude_re, needs_query in api._CAPREAD_RULES:
            if match_re.search(text) and not (
                    exclude_re is not None and exclude_re.search(text)):
                owners[text] = tool_name
                break
        else:
            owners[text] = household_reads.match(text)
    assert all(owners.values()), f"unowned phrasings: {owners}"
    assert owners["whats the status of the house"] == "house_status"
    assert owners["is the vault locked"] == "vault.status"
    assert owners["vault status"] == "vault.status"
