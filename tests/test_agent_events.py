"""Tests for event inflow — the agent team's senses (orchestration Phase 1).

Covers the four pieces that must hold with no HA, no Ollama and no socket:

* :func:`puffpai.agent_events.submit_agent_event` — routing via a stubbed bus,
  the bounded JSONL log, and the never-raises contract.
* Concierge ``intent="event"`` classification, engine-less and deterministic.
* :mod:`puffpai.ha_events` filtering / debounce / event-type derivation against
  a fake WebSocket.
* The Device Sentinel hook firing on a new-device alert.

Run: pytest tests/test_agent_events.py -q
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

from puffpai import agent_events, ha_bridge, ha_events
from puffpai.agents import Budget, Envelope
from puffpai.agents.concierge import ConciergeAgent, is_night
from puffpai.ha_bridge import HABridgeError


# ── fixtures ────────────────────────────────────────────────────────

class StubBus:
    """Minimal AgentBus stand-in that records sends and returns a canned reply."""

    def __init__(self, reply=None, agents=("concierge", "steward", "sentinel")):
        self.reply = reply or {"ok": True, "decision": {"severity": "info"}}
        self.sent: list[tuple[Envelope, str]] = []
        self._agents = set(agents)

    def send(self, envelope, to):
        self.sent.append((envelope, to))
        return self.reply

    def get(self, name):
        return object() if name in self._agents else None


@pytest.fixture(autouse=True)
def _neutralized_legacy_event_log(monkeypatch, tmp_path):
    """Drop the cached path and neutralise the legacy log for every case.

    This checkout really holds ``src/puffpai/data/agent_events.jsonl`` (the
    legacy default), so any case that exercises the env-unset default
    with ``_LEGACY_DATA_DIR`` still pointing there would silently depend on
    real state in the working tree — and would flip its answer the day someone
    deletes that file. Legacy adoption is opt-in per test, never ambient: the
    cases that want it repoint the attribute at a file they seeded themselves.
    """
    monkeypatch.setattr(agent_events, "_LEGACY_DATA_DIR",
                        str(tmp_path / "absent_legacy"))
    monkeypatch.setattr(agent_events, "EVENT_LOG_PATH", None)
    monkeypatch.setattr(agent_events, "_line_count", None)


@pytest.fixture
def event_log(tmp_path, monkeypatch):
    """Point the event log at a temp file and reset the in-process line count."""
    path = tmp_path / "agent_events.jsonl"
    monkeypatch.setattr(agent_events, "EVENT_LOG_PATH", str(path))
    monkeypatch.setattr(agent_events, "_line_count", None)
    return path


@pytest.fixture
def stub_bus(monkeypatch):
    bus = StubBus()
    monkeypatch.setattr(agent_events, "_bus", bus)
    return bus


# ── submit_agent_event ──────────────────────────────────────────────

def test_submit_builds_a_read_only_budgeted_envelope(event_log, stub_bus):
    reply = agent_events.submit_agent_event(
        "ha", "camera_person", {"entity_id": "binary_sensor.shed_person"})

    assert reply == stub_bus.reply
    envelope, to = stub_bus.sent[0]
    assert to == "concierge"
    assert envelope.intent == "event"
    assert envelope.state_changing is False
    assert envelope.requester == "system"
    assert envelope.origin_agent == "ha"
    assert envelope.budget.max_actions == 2
    assert envelope.budget.max_seconds == 15.0
    assert envelope.budget.allow_egress is False
    assert envelope.payload["event_type"] == "camera_person"


def test_submit_appends_a_log_entry_with_the_decision(event_log, stub_bus):
    agent_events.submit_agent_event(
        "ha", "camera_motion", {"entity_id": "binary_sensor.shed_motion"})

    entries = agent_events.read_events(10)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["source"] == "ha"
    assert entry["event_type"] == "camera_motion"
    assert entry["entity_id"] == "binary_sensor.shed_motion"
    assert entry["decision"] == stub_bus.reply
    assert entry["ts"]


def test_read_events_is_newest_first_and_limited(event_log, stub_bus):
    for i in range(5):
        agent_events.submit_agent_event("ha", f"event_{i}", {})
    entries = agent_events.read_events(3)
    assert [e["event_type"] for e in entries] == ["event_4", "event_3", "event_2"]


def test_event_log_is_bounded(event_log, stub_bus, monkeypatch):
    monkeypatch.setattr(agent_events, "MAX_EVENT_LOG_ENTRIES", 10)
    for i in range(25):
        agent_events.submit_agent_event("ha", f"event_{i}", {})
    lines = event_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) <= 10
    assert json.loads(lines[-1])["event_type"] == "event_24"


def test_oversized_payload_is_truncated_in_the_log(event_log, stub_bus):
    agent_events.submit_agent_event("ha", "ha_state_change",
                                    {"blob": "x" * (agent_events.MAX_DATA_BYTES + 10)})
    entry = agent_events.read_events(1)[0]
    assert entry["data"] == {"_truncated": True, "keys": ["blob"]}


# ── event-log location (deployment integrity) ───────────────────────
#
# The appliance runs under ProtectSystem=strict, so the package-relative
# default (<pkg>/data/agent_events.jsonl) is read-only there: every event was
# routed and decided correctly but nothing was ever persisted, behind a
# per-event warning. The path must therefore be reachable from config.

def _config_with_events_dir(tmp_path, events_dir):
    """Build a real PuffPAIConfig whose paths.agent_events_dir is *events_dir*."""
    import json as _json
    from puffpai.config import _build_config
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "puffpai_config.json").write_text(
        _json.dumps({"paths": {"agent_events_dir": str(events_dir)}}))
    return _build_config(str(tmp_path))


@pytest.fixture
def unresolved_event_log(monkeypatch):
    """Clear the cached path so the next call re-resolves through config."""
    monkeypatch.setattr(agent_events, "EVENT_LOG_PATH", None)
    monkeypatch.setattr(agent_events, "_line_count", None)
    from puffpai.logging_config import reset_unwritable_reports
    reset_unwritable_reports()
    yield
    reset_unwritable_reports()


def test_absolute_configured_dir_is_used_and_round_trips(
        tmp_path, monkeypatch, stub_bus, unresolved_event_log):
    """An absolute paths.agent_events_dir wins, and events survive it."""
    events_dir = tmp_path / "data" / "puffpai" / "events"
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "ignored"))
    monkeypatch.setattr("puffpai.config._instance",
                        _config_with_events_dir(tmp_path, events_dir))

    agent_events.submit_agent_event("ha", "camera_person", {"entity_id": "x.y"})

    assert agent_events.get_event_log_path() == str(events_dir / "agent_events.jsonl")
    assert (events_dir / "agent_events.jsonl").is_file()
    entries = agent_events.read_events(10)
    assert len(entries) == 1
    assert entries[0]["event_type"] == "camera_person"
    assert entries[0]["entity_id"] == "x.y"


def test_env_var_places_the_log_when_config_is_silent(
        tmp_path, monkeypatch, stub_bus, unresolved_event_log):
    """$PUFFPAI_DATA_DIR still relocates the log for an unconfigured box."""
    from puffpai.config import _build_config
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "puffpai_config.json").write_text("{}")
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("puffpai.config._instance", _build_config(str(tmp_path)))

    agent_events.submit_agent_event("nodered", "weather_alert", {})

    assert agent_events.get_event_log_path() == str(
        tmp_path / "state" / "agent_events.jsonl")
    assert agent_events.read_events(1)[0]["event_type"] == "weather_alert"


def test_unwritable_log_reports_once_and_still_decides(
        tmp_path, monkeypatch, caplog, stub_bus, unresolved_event_log):
    """A read-only state dir yields one ERROR, no raise, and live decisions."""
    # A file where a directory should be: os.makedirs fails with an OSError,
    # the same class of failure EROFS produces on the appliance.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    events_dir = blocker / "events"
    monkeypatch.setattr("puffpai.config._instance",
                        _config_with_events_dir(tmp_path, events_dir))

    with caplog.at_level("ERROR", logger="puffpai.agent_events"):
        first = agent_events.submit_agent_event("ha", "camera_person", {})
        for _ in range(5):
            agent_events.submit_agent_event("ha", "camera_motion", {})

    # The decision still comes back from the bus — the log is what degrades.
    assert first == stub_bus.reply
    assert len(stub_bus.sent) == 6

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1, f"expected one loud error, got {len(errors)}"
    message = errors[0].getMessage()
    assert str(events_dir) in message          # names the offending path
    assert "ReadWritePaths" in message         # names the likely cause

    # Nothing was persisted, and read_events degrades to empty rather than raising.
    assert agent_events.read_events(10) == []


# ── degraded tiers: config missing or broken ────────────────────────
#
# Neither tier may change *where* $PUFFPAI_DATA_DIR points. The old code read
# the env var raw when the config failed to build, so a relative or
# "~"-prefixed value put the log somewhere no other module looked (and, under
# ProtectSystem=strict, somewhere unwritable).

def _config_that_explodes(monkeypatch):
    """Make get_config() raise, leaving puffpai.config itself importable."""
    def _boom():
        raise RuntimeError("secrets provider unavailable")
    monkeypatch.setattr("puffpai.config.get_config", _boom)


def test_config_failure_joins_a_relative_env_value_to_the_repo_root(monkeypatch):
    _config_that_explodes(monkeypatch)
    monkeypatch.setenv("PUFFPAI_DATA_DIR", "relstate")

    assert agent_events._resolve_event_log_path() == os.path.join(
        agent_events._REPO_ROOT, "relstate", "agent_events.jsonl")


def test_config_failure_expands_a_tilde_env_value(monkeypatch):
    _config_that_explodes(monkeypatch)
    monkeypatch.setenv("PUFFPAI_DATA_DIR", "~/puff_state")

    resolved = agent_events._resolve_event_log_path()
    assert "~" not in resolved
    assert resolved == os.path.join(
        os.path.expanduser("~"), "puff_state", "agent_events.jsonl")


def test_config_failure_ignores_a_whitespace_only_env_value(monkeypatch):
    _config_that_explodes(monkeypatch)
    monkeypatch.setenv("PUFFPAI_DATA_DIR", "   ")

    assert agent_events._resolve_event_log_path() == os.path.join(
        agent_events._DEFAULT_DATA_DIR, "agent_events.jsonl")


# ── the default's move to <repo>/data, and legacy adoption ──────────
#
# The env-unset default is the package-relative src/puffpai/data moved to
# <repo>/data, converging with ha_device_watcher,
# zone3/policy_engine and security/consent_gate. Event history a
# dev box already holds at the old path must not be orphaned by that move,
# GET /agents/events would come back empty, so the old FILE is adopted in
# place: read from and appended to where it already is, never copied, never
# moved. Adoption is suppressed by any explicit choice (config path or
# $PUFFPAI_DATA_DIR), tested as "is it set", never as "did the directory move".

def _silent_config(monkeypatch, *, agent_events_dir="", state_dir="",
                   repo_root=None):
    """Install a config stub whose paths are exactly as given (blank = unset)."""
    from types import SimpleNamespace
    cfg = SimpleNamespace(
        paths=SimpleNamespace(agent_events_dir=agent_events_dir,
                              state_dir=state_dir),
        REPO_ROOT=repo_root or agent_events._REPO_ROOT,
    )
    monkeypatch.setattr("puffpai.config._instance", cfg)
    return cfg


def _seed_legacy(tmp_path, monkeypatch, *, line='{"event_type": "historic"}'):
    """Seed a legacy event log under tmp_path and point the module at it.

    Returns ``(legacy_file, new_data_dir)``. The new data dir deliberately does
    not exist, which is the only state in which adoption may fire.
    """
    legacy_dir = tmp_path / "src" / "puffpai" / "data"
    legacy_dir.mkdir(parents=True)
    legacy = legacy_dir / "agent_events.jsonl"
    legacy.write_text(line + "\n", encoding="utf-8")
    new_dir = tmp_path / "data"
    monkeypatch.setattr(agent_events, "_LEGACY_DATA_DIR", str(legacy_dir))
    monkeypatch.setattr(agent_events, "_DEFAULT_DATA_DIR", str(new_dir))
    return legacy, new_dir


def test_env_unset_lands_in_the_repo_root_data_dir(monkeypatch):
    """The default is <repo>/data — not the old package-relative src/puffpai/data."""
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    _silent_config(monkeypatch)

    assert agent_events._DEFAULT_DATA_DIR == os.path.join(
        agent_events._REPO_ROOT, "data")
    assert agent_events._resolve_event_log_path() == os.path.join(
        agent_events._REPO_ROOT, "data", "agent_events.jsonl")


def test_legacy_log_is_adopted_and_appended_to_in_place(
        tmp_path, monkeypatch, stub_bus):
    """The 185 KB regression: history is kept, and new events join it.

    Nothing may be created at the new path — a copy would fork the log, and a
    move would take a migration decision out of the operator's hands.
    """
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    legacy, new_dir = _seed_legacy(tmp_path, monkeypatch)
    _silent_config(monkeypatch, repo_root=str(tmp_path))

    assert agent_events.get_event_log_path() == str(legacy)

    agent_events.submit_agent_event("ha", "camera_person", {"entity_id": "x.y"})

    lines = legacy.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2, "the pre-existing history must still be there"
    assert json.loads(lines[0])["event_type"] == "historic"
    assert json.loads(lines[1])["event_type"] == "camera_person"
    assert not new_dir.exists(), "nothing may be written at the new path"


def test_a_log_at_the_new_path_wins_over_legacy(tmp_path, monkeypatch):
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    legacy, new_dir = _seed_legacy(tmp_path, monkeypatch)
    new_dir.mkdir()
    (new_dir / "agent_events.jsonl").write_text("", encoding="utf-8")
    _silent_config(monkeypatch, repo_root=str(tmp_path))

    assert agent_events._resolve_event_log_path() == str(
        new_dir / "agent_events.jsonl")


def test_an_absolute_env_dir_suppresses_adoption(tmp_path, monkeypatch):
    legacy, _new_dir = _seed_legacy(tmp_path, monkeypatch)
    env_dir = tmp_path / "state"
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(env_dir))
    _silent_config(monkeypatch, repo_root=str(tmp_path))

    assert agent_events._resolve_event_log_path() == str(
        env_dir / "agent_events.jsonl")


@pytest.mark.parametrize("form", ["absolute", "relative"])
def test_env_pointed_at_the_default_dir_still_suppresses_adoption(
        form, tmp_path, monkeypatch):
    """Rule: the env test is "is it set", never a comparison of resolved dirs.

    An operator who points $PUFFPAI_DATA_DIR at the default data dir — the
    absolute <repo>/data, or the relative "data", which joins to the repo root
    — resolves to the very directory the module ships with. Deciding by path
    comparison would read that as "unset", adopt, and then APPEND to the
    read-only code tree while an env var explicitly said otherwise.
    """
    legacy, new_dir = _seed_legacy(tmp_path, monkeypatch)
    monkeypatch.setenv("PUFFPAI_DATA_DIR",
                       str(new_dir) if form == "absolute" else "data")
    _silent_config(monkeypatch, repo_root=str(tmp_path))

    resolved = agent_events._resolve_event_log_path()
    assert resolved == str(new_dir / "agent_events.jsonl")
    assert resolved != str(legacy)


@pytest.mark.parametrize("env_value", ["", "   ", "\t\n"])
def test_a_blank_env_value_counts_as_unset_for_adoption(
        env_value, tmp_path, monkeypatch):
    legacy, _new_dir = _seed_legacy(tmp_path, monkeypatch)
    monkeypatch.setenv("PUFFPAI_DATA_DIR", env_value)
    _silent_config(monkeypatch, repo_root=str(tmp_path))

    assert agent_events._resolve_event_log_path() == str(legacy)


@pytest.mark.parametrize("field", ["agent_events_dir", "state_dir"])
@pytest.mark.parametrize("shape", ["absolute", "relative"])
def test_a_configured_dir_suppresses_adoption(
        field, shape, tmp_path, monkeypatch):
    """A configured path is an explicit decision, exactly like the env var.

    This module is the only one that honours paths.agent_events_dir /
    paths.state_dir, so it is the only one that has to suppress adoption on
    them as well; otherwise a box with a configured (but not yet created)
    events directory would quietly keep appending to the old in-tree log.
    """
    legacy, _new_dir = _seed_legacy(tmp_path, monkeypatch)
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    configured = (str(tmp_path / "configured") if shape == "absolute"
                  else "configured")
    _silent_config(monkeypatch, repo_root=str(tmp_path), **{field: configured})

    assert agent_events._resolve_event_log_path() == str(
        tmp_path / "configured" / "agent_events.jsonl")


def test_an_explicit_event_log_path_wins_over_adoption(tmp_path, monkeypatch):
    """The caching/override contract: an assigned EVENT_LOG_PATH is final."""
    _seed_legacy(tmp_path, monkeypatch)
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    _silent_config(monkeypatch, repo_root=str(tmp_path))
    override = tmp_path / "override" / "agent_events.jsonl"
    monkeypatch.setattr(agent_events, "EVENT_LOG_PATH", str(override))

    assert agent_events.get_event_log_path() == str(override)


def test_submit_never_raises_when_the_bus_fails(event_log, monkeypatch):
    class Exploding:
        def send(self, envelope, to):
            raise RuntimeError("bus down")

    monkeypatch.setattr(agent_events, "_bus", Exploding())
    reply = agent_events.submit_agent_event("ha", "camera_person", {})
    assert reply["ok"] is False
    assert "bus down" in reply["error"]
    # The failure is still recorded.
    assert agent_events.read_events(1)[0]["decision"]["ok"] is False


def test_submit_degrades_when_the_team_is_disabled(event_log, monkeypatch):
    monkeypatch.setattr(agent_events, "_bus", None)
    monkeypatch.setenv("PUFFPAI_AGENT_TEAM_ENABLED", "0")
    reply = agent_events.submit_agent_event("ha", "camera_person", {})
    assert reply["ok"] is False
    assert "unavailable" in reply["error"]


# ── Concierge event classification (engine-less) ────────────────────

def _event_envelope(event_type: str, data: dict, source: str = "ha") -> Envelope:
    return Envelope(
        intent="event",
        payload={"source": source, "event_type": event_type, "data": data},
        requester="system",
        origin_agent=source,
        budget=Budget(max_actions=2, max_seconds=15.0, allow_egress=False),
        state_changing=False,
    )


def _concierge(bus=None) -> ConciergeAgent:
    agent = ConciergeAgent()
    agent.bind_bus(bus if bus is not None else StubBus(
        reply={"ok": True, "state": "on"}))
    return agent


def test_camera_person_by_day_is_info(monkeypatch):
    monkeypatch.setattr("puffpai.agents.concierge._now",
                        lambda: datetime(2026, 8, 4, 14, 0))
    reply = _concierge().handle(_event_envelope(
        "camera_person", {"entity_id": "binary_sensor.shed_person",
                          "friendly_name": "Driveway"}))
    decision = reply["decision"]
    assert reply["ok"] is True
    assert decision["severity"] == "info"
    assert decision["suggested_action"] == "logged"
    assert "Driveway" in decision["summary"]
    assert decision["night"] is False


@pytest.mark.parametrize("hour", [22, 23, 0, 3, 5])
def test_camera_person_at_night_is_attention(monkeypatch, hour):
    monkeypatch.setattr("puffpai.agents.concierge._now",
                        lambda: datetime(2026, 8, 4, hour, 0))
    reply = _concierge().handle(_event_envelope(
        "camera_person", {"entity_id": "binary_sensor.shed_person"}))
    assert reply["decision"]["severity"] == "attention"
    assert reply["decision"]["night"] is True


def test_camera_motion_at_night_stays_info(monkeypatch):
    monkeypatch.setattr("puffpai.agents.concierge._now",
                        lambda: datetime(2026, 8, 4, 2, 0))
    reply = _concierge().handle(_event_envelope(
        "camera_motion", {"entity_id": "binary_sensor.shed_motion"}))
    assert reply["decision"]["severity"] == "info"


def test_camera_event_asks_the_steward_for_current_state():
    bus = StubBus(reply={"ok": True, "state": "on"})
    reply = _concierge(bus).handle(_event_envelope(
        "camera_person", {"entity_id": "binary_sensor.shed_person"}))
    assert reply["decision"]["entity_state"] == "on"
    assert [to for _, to in bus.sent] == ["steward"]
    sub, _ = bus.sent[0]
    assert sub.intent == "device_status"
    assert sub.state_changing is False


def test_new_device_event_is_attention_and_asks_sentinel():
    bus = StubBus(reply={"ok": True, "status": {"pending_alerts": 2}})
    reply = _concierge(bus).handle(_event_envelope(
        "network_new_device", {"mac": "aa:bb:cc:dd:ee:ff", "name": "unknown-42"},
        source="device_sentinel"))
    decision = reply["decision"]
    assert decision["severity"] == "attention"
    assert "unknown-42" in decision["summary"]
    assert "2 pending alert" in decision["summary"]
    assert [to for _, to in bus.sent] == ["sentinel"]


def test_weather_alert_with_alerts_is_attention():
    reply = _concierge().handle(_event_envelope(
        "weather_alert",
        {"count": 2, "headlines": ["Flood Warning", "Heat Advisory"]},
        source="nodered"))
    decision = reply["decision"]
    assert decision["severity"] == "attention"
    assert decision["count"] == 2
    assert "Flood Warning" in decision["summary"]


def test_weather_alert_headlines_are_capped_at_five():
    reply = _concierge().handle(_event_envelope(
        "weather_alert", {"count": 9, "headlines": [f"a{i}" for i in range(9)]},
        source="nodered"))
    assert len(reply["decision"]["headlines"]) == 5


def test_weather_alert_with_no_alerts_is_info():
    reply = _concierge().handle(_event_envelope(
        "weather_alert", {"count": 0, "headlines": []}, source="nodered"))
    assert reply["decision"]["severity"] == "info"


def test_unknown_event_type_is_logged_info():
    reply = _concierge().handle(_event_envelope("meteor_strike", {}))
    decision = reply["decision"]
    assert decision["severity"] == "info"
    assert decision["suggested_action"] == "logged"
    assert "meteor_strike" in decision["summary"]


def test_event_classification_stays_inside_the_envelope_budget():
    bus = StubBus(reply={"ok": True, "state": "on"})
    envelope = _event_envelope("camera_person",
                               {"entity_id": "binary_sensor.shed_person"})
    _concierge(bus).handle(envelope)
    assert envelope.budget.actions_used <= envelope.budget.max_actions


def test_event_classification_survives_a_crashing_lookup():
    class Exploding(StubBus):
        def send(self, envelope, to):
            raise RuntimeError("steward is down")

    reply = _concierge(Exploding()).handle(_event_envelope(
        "camera_person", {"entity_id": "binary_sensor.shed_person"}))
    assert reply["ok"] is True
    assert reply["decision"]["entity_state"] is None


def test_is_night_window_boundaries():
    assert is_night(datetime(2026, 8, 4, 22, 0)) is True
    assert is_night(datetime(2026, 8, 4, 5, 59)) is True
    assert is_night(datetime(2026, 8, 4, 6, 0)) is False
    assert is_night(datetime(2026, 8, 4, 21, 59)) is False


# ── entity_id validation: the event path must not become an HA read ─
#
# A pushed event carries an attacker-influenced entity_id. Before this was
# validated, ``{"entity_id": "../../auth/providers"}`` reached
# ``HABridge.get_entity_state`` and requests resolved the dot segments into a
# token-authenticated GET against an arbitrary HA endpoint. Three layers now
# reject it; each of these tests pins one of them.

TRAVERSAL_IDS = [
    "../../auth/providers",
    "sensor.a/../../error_log",
    "x?filter=1",
    "sensor.a?filter=1",
    "SENSOR.Foo",
    "sensor.Foo",
    "sensor",
    "sensor.",
    ".hidden",
    "sensor.a b",
    "sensor.a\nb",
    "sensor.foo\n",          # ``$`` would have allowed this trailing newline
    "sensor.foo/",
    "",
]


@pytest.mark.parametrize("entity_id", [
    "binary_sensor.driveway_person",
    "sensor.back_porch_temperature",
    "light.kitchen",
    "_private.thing",
])
def test_valid_entity_ids_are_accepted(entity_id):
    assert ha_bridge.is_valid_entity_id(entity_id) is True
    assert ha_bridge.validate_entity_id(entity_id) == entity_id


@pytest.mark.parametrize("entity_id", TRAVERSAL_IDS)
def test_malformed_entity_ids_are_rejected(entity_id):
    assert ha_bridge.is_valid_entity_id(entity_id) is False
    with pytest.raises(HABridgeError):
        ha_bridge.validate_entity_id(entity_id)


@pytest.mark.parametrize("entity_id", [None, 42, ["sensor.a"], {"a": 1}, True])
def test_non_string_entity_ids_are_rejected(entity_id):
    assert ha_bridge.is_valid_entity_id(entity_id) is False
    with pytest.raises(HABridgeError):
        ha_bridge.validate_entity_id(entity_id)


def test_rejection_message_does_not_echo_the_whole_input():
    with pytest.raises(HABridgeError) as exc:
        ha_bridge.validate_entity_id("../../auth/providers/and_more_secrets")
    assert "and_more_secrets" not in str(exc.value)


@pytest.mark.parametrize("segment", ["light", "turn_on", "_x"])
def test_valid_service_segments_are_accepted(segment):
    assert ha_bridge.validate_segment(segment) == segment


@pytest.mark.parametrize("segment", [
    "../../auth", "light/..", "light.turn_on", "Light", "turn on", "", None, 7,
])
def test_malformed_service_segments_are_rejected(segment):
    with pytest.raises(HABridgeError):
        ha_bridge.validate_segment(segment)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.ok = True
        self.headers = {"Content-Type": "application/json"}
        self.content = b""

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class RecordingSession:
    """requests.Session stand-in that records every URL it is asked for."""

    def __init__(self, payload=None):
        self.calls: list[tuple[str, str]] = []
        self._payload = payload if payload is not None else {"state": "on"}
        self.headers: dict = {}

    def get(self, url, **_kwargs):
        self.calls.append(("GET", url))
        return FakeResponse(self._payload)

    def post(self, url, **_kwargs):
        self.calls.append(("POST", url))
        return FakeResponse(self._payload)

    def delete(self, url, **_kwargs):
        self.calls.append(("DELETE", url))
        return FakeResponse(self._payload)


def _bridge(payload=None) -> tuple[ha_bridge.HABridge, RecordingSession]:
    """An HABridge with no config and no socket, wired to a recording session."""
    bridge = ha_bridge.HABridge.__new__(ha_bridge.HABridge)
    bridge._url = "http://ha.test:8123"
    bridge._token = "test-token"
    bridge._enabled = True
    bridge._cache = ha_bridge._StateCache(ttl=0.0)
    session = RecordingSession(payload)
    bridge._session = session
    return bridge, session


@pytest.mark.parametrize("entity_id", TRAVERSAL_IDS)
def test_get_entity_state_makes_no_request_for_a_bad_id(entity_id):
    bridge, session = _bridge()
    with pytest.raises(HABridgeError):
        bridge.get_entity_state(entity_id)
    assert session.calls == []


def test_get_entity_state_requests_the_expected_url():
    bridge, session = _bridge({"state": "on"})
    assert bridge.get_entity_state("binary_sensor.shed_person") == {"state": "on"}
    assert session.calls == [
        ("GET", "http://ha.test:8123/api/states/binary_sensor.shed_person")]


@pytest.mark.parametrize("entity_id", ["../../auth/providers", "x?filter=1"])
def test_camera_image_makes_no_request_for_a_bad_id(entity_id):
    bridge, session = _bridge()
    with pytest.raises(HABridgeError):
        bridge.get_camera_image(entity_id)
    assert session.calls == []


@pytest.mark.parametrize("domain,service", [
    ("../../auth", "providers"),
    ("light", "../../../config"),
    ("light", "turn_on/../.."),
    ("Light", "turn_on"),
    ("light", ""),
])
def test_call_service_makes_no_request_for_bad_segments(domain, service):
    bridge, session = _bridge()
    with pytest.raises(HABridgeError):
        bridge.call_service(domain, service, entity_id="light.kitchen")
    assert session.calls == []


def test_call_service_requests_the_expected_url():
    bridge, session = _bridge([])
    bridge.call_service("light", "turn_on", entity_id="light.kitchen")
    assert session.calls == [
        ("POST", "http://ha.test:8123/api/services/light/turn_on")]


@pytest.mark.parametrize("method,args", [
    ("control_device", ("../../auth/providers", "turn_on")),
    ("activate_scene", ("../../auth/providers",)),
    ("trigger_automation", ("../../auth/providers",)),
    ("set_light", ("../../auth/providers",)),
    ("set_climate", ("../../auth/providers",)),
    ("get_device", ("../../auth/providers",)),
    ("get_history", ("../../auth/providers",)),
])
def test_control_helpers_make_no_request_for_a_bad_id(method, args):
    bridge, session = _bridge()
    with pytest.raises(HABridgeError):
        getattr(bridge, method)(*args)
    assert session.calls == []


def test_history_rejects_a_traversing_start_time():
    bridge, session = _bridge()
    with pytest.raises(HABridgeError):
        bridge.get_history("sensor.temp", start="../../auth/providers")
    assert session.calls == []


def test_request_refuses_dot_segments_even_if_a_validator_is_bypassed():
    """Bottom layer: an assembled endpoint with dot segments never goes out."""
    bridge, session = _bridge()
    with pytest.raises(HABridgeError):
        bridge._request("states/../../auth/providers")
    assert session.calls == []


# ── Concierge: malformed ids never reach the Steward ────────────────

@pytest.mark.parametrize("entity_id", TRAVERSAL_IDS + [42, ["a"], {"a": 1}])
def test_malformed_entity_id_yields_an_info_decision_with_no_ha_lookup(entity_id):
    bus = StubBus(reply={"ok": True, "state": {"state": "on"}})
    reply = _concierge(bus).handle(_event_envelope(
        "camera_person", {"entity_id": entity_id}))
    decision = reply["decision"]
    assert reply["ok"] is True
    assert decision["severity"] == "info"
    assert decision["entity_id"] is None
    assert decision["entity_state"] is None
    assert "invalid entity id" in decision["summary"]
    assert bus.sent == []   # zero Steward asks


def test_empty_entity_id_still_classifies_without_a_lookup():
    """An absent id is not an attack — it just means "no state to fetch"."""
    bus = StubBus(reply={"ok": True, "state": {"state": "on"}})
    reply = _concierge(bus).handle(_event_envelope("camera_person", {}))
    assert reply["decision"]["severity"] in ("info", "attention")
    assert bus.sent == []


def test_summary_never_dumps_the_upstream_ha_body():
    """entity_state keeps the payload; the summary quotes known fields only."""
    body = {
        "entity_id": "binary_sensor.shed_person",
        "state": "on",
        "attributes": {"friendly_name": "Shed", "device_id": "abcdef0123456789"},
        "context": {"user_id": "leaky-user-id"},
    }
    bus = StubBus(reply={"ok": True, "state": body})
    reply = _concierge(bus).handle(_event_envelope(
        "camera_person", {"entity_id": "binary_sensor.shed_person"}))
    decision = reply["decision"]
    summary = decision["summary"]
    assert decision["entity_state"] == body          # still available to callers
    assert "leaky-user-id" not in summary
    assert "abcdef0123456789" not in summary
    assert "current state on" in summary


def test_valid_camera_person_still_works_end_to_end(monkeypatch):
    """Regression: the happy path is unchanged by the validation layers."""
    monkeypatch.setattr("puffpai.agents.concierge._now",
                        lambda: datetime(2026, 8, 4, 2, 0))

    bridge, session = _bridge({"state": "on",
                               "attributes": {"friendly_name": "Driveway"}})

    class BridgeBackedBus(StubBus):
        def send(self, envelope, to):
            self.sent.append((envelope, to))
            eid = envelope.payload["entity_id"]
            return {"ok": True, "state": bridge.get_entity_state(eid)}

    bus = BridgeBackedBus()
    reply = _concierge(bus).handle(_event_envelope(
        "camera_person", {"entity_id": "binary_sensor.driveway_person",
                          "friendly_name": "Driveway"}))
    decision = reply["decision"]
    assert decision["severity"] == "attention"
    assert decision["entity_id"] == "binary_sensor.driveway_person"
    assert decision["entity_state"]["state"] == "on"
    assert "Driveway" in decision["summary"]
    assert session.calls == [
        ("GET", "http://ha.test:8123/api/states/"
                "binary_sensor.driveway_person")]


# ── ha_events: pure helpers ─────────────────────────────────────────

@pytest.mark.parametrize("entity_id,expected", [
    ("binary_sensor.driveway_person", "camera_person"),
    ("binary_sensor.back_door_motion", "camera_motion"),
    ("sensor.back_porch_temperature", "ha_state_change"),
])
def test_derive_event_type(entity_id, expected):
    assert ha_events.derive_event_type(entity_id) == expected


@pytest.mark.parametrize("entity_id,old,new,expected", [
    ("binary_sensor.shed_person", "off", "on", True),
    ("binary_sensor.shed_person", "on", "off", False),
    ("binary_sensor.shed_person", "unavailable", "on", False),
    ("binary_sensor.shed_person", "off", "unknown", False),
    ("binary_sensor.shed_person", None, "on", False),
    ("binary_sensor.shed_person", "on", "on", False),
    ("sensor.temperature", "70", "71", True),
    ("sensor.temperature", "70", "70", False),
])
def test_should_fire_rules(entity_id, old, new, expected):
    assert ha_events.should_fire(entity_id, old, new) is expected


def test_watchlist_matches_globs():
    patterns = ["binary_sensor.driveway_*", "sensor.back_porch_temperature"]
    assert ha_events.matches_watchlist("binary_sensor.driveway_person", patterns)
    assert ha_events.matches_watchlist("sensor.back_porch_temperature", patterns)
    assert not ha_events.matches_watchlist("light.kitchen", patterns)
    assert not ha_events.matches_watchlist("", patterns)


@pytest.mark.parametrize("http_url,expected", [
    ("http://192.168.50.140:8123", "ws://192.168.50.140:8123/api/websocket"),
    ("https://ha.example.com/", "wss://ha.example.com/api/websocket"),
])
def test_ws_url_derivation(http_url, expected):
    assert ha_events.ws_url_for(http_url) == expected


# ── ha_events: listener behaviour with a fake socket ────────────────

class FakeWS:
    """Scripted WebSocket: replays *frames*, records sends, then closes."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent: list[dict] = []
        self.closed = False

    def recv(self):
        if not self._frames:
            raise ConnectionError("fake socket drained")
        return json.dumps(self._frames.pop(0))

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def close(self):
        self.closed = True


def _state_changed(entity_id, old, new, friendly="Shed"):
    return {
        "event_type": "state_changed",
        "data": {
            "entity_id": entity_id,
            "old_state": {"state": old} if old is not None else None,
            "new_state": {"state": new,
                          "attributes": {"friendly_name": friendly}},
        },
    }


def _listener(**kwargs):
    calls: list[tuple] = []
    listener = ha_events.HAEventListener(
        url="http://192.168.50.140:8123",
        token="test-token",
        watch_entities=kwargs.pop("watch_entities",
                                  ["binary_sensor.driveway_*"]),
        debounce_seconds=kwargs.pop("debounce_seconds", 30),
        submit=lambda *a: calls.append(a),
        **kwargs,
    )
    return listener, calls


def test_handshake_authenticates_then_subscribes():
    ws = FakeWS([{"type": "auth_required"}, {"type": "auth_ok"}])
    listener, _ = _listener(ws_factory=lambda url: ws)
    listener._connect()
    assert ws.sent[0] == {"type": "auth", "access_token": "test-token"}
    assert ws.sent[1]["type"] == "subscribe_events"
    assert ws.sent[1]["event_type"] == "state_changed"


def test_failed_auth_raises_without_echoing_the_response():
    ws = FakeWS([{"type": "auth_required"},
                 {"type": "auth_invalid", "message": "test-token"}])
    listener, _ = _listener(ws_factory=lambda url: ws)
    with pytest.raises(RuntimeError) as exc:
        listener._connect()
    assert "test-token" not in str(exc.value)


def test_watched_entity_turning_on_is_submitted():
    listener, calls = _listener()
    fired = listener.handle_state_changed(
        _state_changed("binary_sensor.driveway_person", "off", "on"))
    assert fired is True
    source, event_type, data = calls[0]
    assert (source, event_type) == ("ha", "camera_person")
    assert data == {
        "entity_id": "binary_sensor.driveway_person",
        "old_state": "off",
        "new_state": "on",
        "friendly_name": "Shed",
    }


def test_unwatched_entity_is_ignored():
    listener, calls = _listener()
    assert listener.handle_state_changed(
        _state_changed("light.kitchen", "off", "on")) is False
    assert calls == []


def test_binary_sensor_off_edge_is_ignored():
    listener, calls = _listener()
    assert listener.handle_state_changed(
        _state_changed("binary_sensor.driveway_person", "on", "off")) is False
    assert calls == []


def test_unavailable_transitions_are_ignored():
    listener, calls = _listener()
    assert listener.handle_state_changed(
        _state_changed("binary_sensor.driveway_person",
                       "unavailable", "on")) is False
    assert calls == []


def test_debounce_collapses_repeat_triggers():
    listener, calls = _listener(debounce_seconds=3600)
    event = _state_changed("binary_sensor.driveway_person", "off", "on")
    assert listener.handle_state_changed(event) is True
    assert listener.handle_state_changed(event) is False
    assert len(calls) == 1


def test_debounce_is_per_entity():
    listener, calls = _listener(debounce_seconds=3600)
    listener.handle_state_changed(
        _state_changed("binary_sensor.driveway_person", "off", "on"))
    listener.handle_state_changed(
        _state_changed("binary_sensor.driveway_motion", "off", "on"))
    assert len(calls) == 2


def test_pump_dispatches_event_frames_only():
    frames = [
        {"id": 1, "type": "result", "success": True},
        {"id": 1, "type": "event",
         "event": _state_changed("binary_sensor.driveway_person",
                                 "off", "on")},
    ]
    listener, calls = _listener()
    with pytest.raises(ConnectionError):
        listener._pump(FakeWS(frames))
    assert len(calls) == 1


def test_start_listener_is_disabled_when_ha_events_is_off(monkeypatch):
    class Cfg:
        class ha_events:  # noqa: N801 - mirrors the config attribute name
            enabled = False

    monkeypatch.setattr(ha_events, "_listener", None)
    monkeypatch.setattr("puffpai.config.get_config", lambda: Cfg)
    assert ha_events.start_listener() is None


def test_start_listener_is_not_started_with_nothing_to_watch(monkeypatch):
    class Cfg:
        class ha_events:  # noqa: N801 - mirrors the config attribute name
            enabled = True
            watch_entities: list = []
            debounce_seconds = 30

    resolved = []

    def _ha():
        resolved.append(True)
        return "http://ha.test:8123", "token", True

    monkeypatch.setattr(ha_events, "_listener", None)
    monkeypatch.setattr("puffpai.config.get_config", lambda: Cfg)
    monkeypatch.setattr(ha_events, "_resolve_ha", _ha)
    assert ha_events.start_listener() is None
    assert resolved == [], "an empty watch list must not reach HA at all"


# ── Device Sentinel hook ────────────────────────────────────────────

def test_new_device_alert_submits_an_agent_event(tmp_path, monkeypatch):
    from puffpai import device_sentinel as ds

    calls: list[tuple] = []
    monkeypatch.setattr(agent_events, "submit_agent_event",
                        lambda *a: calls.append(a) or {"ok": True})
    monkeypatch.setattr(ds, "_ALERTS_FILE", tmp_path / "alerts.jsonl")

    sentinel = ds.DeviceSentinel()
    monkeypatch.setattr(sentinel, "_quarantine", lambda fp: (False, "disabled"))
    sentinel._handle_new_device(ds.Fingerprint(
        source="ip", key="aa:bb:cc:dd:ee:ff", label="unknown-42",
        extra={"ip": "192.168.50.99"}))

    assert len(calls) == 1
    source, event_type, data = calls[0]
    assert (source, event_type) == ("device_sentinel", "network_new_device")
    assert data["mac"] == "aa:bb:cc:dd:ee:ff"
    assert data["ip"] == "192.168.50.99"
    assert data["name"] == "unknown-42"


def test_sentinel_alert_survives_a_failing_submit(tmp_path, monkeypatch):
    from puffpai import device_sentinel as ds

    def boom(*_args):
        raise RuntimeError("agent team exploded")

    monkeypatch.setattr(agent_events, "submit_agent_event", boom)
    monkeypatch.setattr(ds, "_ALERTS_FILE", tmp_path / "alerts.jsonl")

    sentinel = ds.DeviceSentinel()
    monkeypatch.setattr(sentinel, "_quarantine", lambda fp: (False, "disabled"))
    sentinel._handle_new_device(ds.Fingerprint(
        source="ip", key="aa:bb:cc:dd:ee:ff", label="unknown-42"))

    assert len(sentinel.list_alerts()) == 1


# ── escalation notify hook ──────────────────────────────────────────
#
# A fresh needs_human / attention decision must reach a person through the
# Steward's notify path, on the bus; duplicates and repeats within the rate
# window must not. The hook never raises into the intake path.

#: The real Concierge event reply: verdict NESTED under "decision" (the shape
#: the first deploy of this hook got wrong — it read the wrapper's top level).
NEEDS_HUMAN = {"ok": True, "source": "nodered", "event_type": "service_degraded",
               "decision": {"severity": "info", "suggested_action": "needs_human",
                            "duplicate": False,
                            "summary": "2 service(s) down: Ollama LLM, X"}}


@pytest.fixture
def notify_reset(monkeypatch):
    monkeypatch.setattr(agent_events, "_last_notified", {})
    monkeypatch.delenv("PUFFPAI_NOTIFY_SERVICE", raising=False)


def test_needs_human_decision_notifies_steward(event_log, notify_reset, monkeypatch):
    bus = StubBus(reply=dict(NEEDS_HUMAN))
    monkeypatch.setattr(agent_events, "_bus", bus)

    agent_events.submit_agent_event("nodered", "service_degraded", {})

    assert len(bus.sent) == 2, "expected event send + notify send"
    envelope, to = bus.sent[1]
    assert to == "steward"
    assert envelope.intent == "notify_user"
    assert envelope.state_changing is True
    assert envelope.requester == "system"
    assert envelope.budget.allow_egress is False
    assert envelope.payload["service"] == agent_events.DEFAULT_NOTIFY_SERVICE
    assert envelope.payload["title"] == "PuffPAI: service degraded"
    assert "2 service(s) down" in envelope.payload["message"]
    assert "needs_human" in envelope.payload["message"]

    entry = agent_events.read_events(1)[0]
    assert entry["notified"] == bus.reply


def test_attention_severity_notifies(event_log, notify_reset, monkeypatch):
    # Flat shape (no wrapper) must keep working too.
    bus = StubBus(reply={"ok": True, "severity": "attention", "duplicate": False,
                         "suggested_action": "announce", "summary": "person at shed"})
    monkeypatch.setattr(agent_events, "_bus", bus)

    agent_events.submit_agent_event("ha", "camera_person", {})

    assert len(bus.sent) == 2
    assert bus.sent[1][1] == "steward"


def test_duplicate_decision_never_notifies(event_log, notify_reset, monkeypatch):
    bus = StubBus(reply={"ok": True, "event_type": "service_degraded",
                         "decision": {"severity": "info", "duplicate": True,
                                      "suggested_action": "logged",
                                      "escalated_action": "needs_human"}})
    monkeypatch.setattr(agent_events, "_bus", bus)

    agent_events.submit_agent_event("nodered", "service_degraded", {})

    assert len(bus.sent) == 1, "duplicate must not re-notify"
    assert "notified" not in agent_events.read_events(1)[0]


def test_info_decision_does_not_notify(event_log, stub_bus, notify_reset):
    agent_events.submit_agent_event("ha", "camera_motion", {})
    assert len(stub_bus.sent) == 1


def test_notify_rate_limited_within_window(event_log, notify_reset, monkeypatch):
    bus = StubBus(reply=dict(NEEDS_HUMAN))
    monkeypatch.setattr(agent_events, "_bus", bus)

    agent_events.submit_agent_event("nodered", "service_degraded", {})
    agent_events.submit_agent_event("nodered", "service_degraded", {})

    # 2 event sends + exactly 1 notify: the second is rate-limited.
    assert len(bus.sent) == 3
    newest = agent_events.read_events(1)[0]
    assert newest["notified"] == {"skipped": "rate_limited"}


def test_notify_service_env_override(event_log, notify_reset, monkeypatch):
    bus = StubBus(reply=dict(NEEDS_HUMAN))
    monkeypatch.setattr(agent_events, "_bus", bus)
    monkeypatch.setenv("PUFFPAI_NOTIFY_SERVICE", "mobile_app_sam")

    agent_events.submit_agent_event("nodered", "service_degraded", {})

    assert bus.sent[1][0].payload["service"] == "mobile_app_sam"


def test_notify_failure_never_breaks_intake(event_log, notify_reset, monkeypatch):
    class ExplodingSteward(StubBus):
        def send(self, envelope, to):
            if to == "steward":
                raise RuntimeError("steward wedged")
            return super().send(envelope, to)

    bus = ExplodingSteward(reply=dict(NEEDS_HUMAN))
    monkeypatch.setattr(agent_events, "_bus", bus)

    reply = agent_events.submit_agent_event("nodered", "service_degraded", {})

    assert reply == bus.reply, "intake must return the decision regardless"
    entry = agent_events.read_events(1)[0]
    assert entry["notified"]["ok"] is False
    assert "steward wedged" in entry["notified"]["error"]
