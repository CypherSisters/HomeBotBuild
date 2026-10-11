"""Config/state self-check — orchestration Phase 5b.

Two halves, both fully offline:

* :mod:`puffpai.self_check` — every detector against a synthetic filesystem or
  a stub bridge, the "``repair=False`` changes nothing" contract, and repair
  idempotence, and
* the Concierge ``self_check`` decider — classification table, dedup window,
  never-raises contract.

Nothing here touches the live house, a live service, or a credential: the Home
Assistant bridge is always a stub, entity ids are fabricated, and every path is
under ``tmp_path``.

Run: pytest tests/test_self_check.py -q
"""

from __future__ import annotations

import os
import time

import pytest

from puffpai import self_check
from puffpai.agents import Budget, Envelope
from puffpai.agents.concierge import (
    SELF_CHECK_INFORMATIONAL,
    SERVICE_DEGRADED_DEDUP_SECONDS,
    ConciergeAgent,
    reset_service_degraded_dedup,
)


# ── helpers ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_dedup():
    reset_service_degraded_dedup()
    yield
    reset_service_degraded_dedup()


@pytest.fixture
def clock(monkeypatch):
    """A pinned monotonic clock the dedup tests can advance."""
    state = {"t": 5000.0}
    monkeypatch.setattr("puffpai.agents.concierge._monotonic",
                        lambda: state["t"])
    return state


def _snapshot(root) -> set:
    """Every path under *root*, for the no-side-effects assertion."""
    out = set()
    for base, dirs, files in os.walk(str(root)):
        for name in list(dirs) + list(files):
            out.add(os.path.join(base, name))
    return out


def _paths_entry(label, path):
    return (label, f"puffpai.fake.{label}", f"the {label} store", lambda: str(path))


def _only(checks, check_id):
    matched = [c for c in checks if c["id"] == check_id]
    assert len(matched) == 1, f"{check_id} not found in {[c['id'] for c in checks]}"
    return matched[0]


class StubBridge:
    """A Home Assistant bridge that never touches the network."""

    def __init__(self, status=None, states=None, raises=None):
        self._status = status if status is not None else {"connected": True}
        self._states = states if states is not None else []
        self._raises = raises

    def get_status(self):
        if self._raises:
            raise self._raises
        return self._status

    def get_all_states(self):
        if self._raises:
            raise self._raises
        return self._states


def _entity(entity_id, state):
    return {"entity_id": entity_id, "state": state, "attributes": {}}


# ── 1. writable state resolving into a read-only path ───────────────

def test_writable_state_dir_reads_ok(tmp_path, monkeypatch):
    good = tmp_path / "state"
    good.mkdir()
    monkeypatch.setattr(self_check, "STATE_PATHS",
                        (_paths_entry("events", good),))
    check = _only(self_check.check_state_paths(), "state_path:events")
    assert check["status"] == "ok"
    assert check["repaired"] is False


def test_existing_but_unwritable_dir_fails_and_is_not_repairable(
        tmp_path, monkeypatch):
    """The ProtectSystem=strict case: exists, but every write is dropped."""
    ro = tmp_path / "readonly"
    ro.mkdir()
    monkeypatch.setattr(self_check, "STATE_PATHS", (_paths_entry("events", ro),))
    # os.access cannot be made to fail portably (Windows ignores dir modes), so
    # the writability primitive itself is substituted — the same seam the
    # module documents.
    monkeypatch.setattr(self_check, "_is_writable", lambda p: False)

    check = _only(self_check.check_state_paths(repair=True),
                  "state_path:events")
    assert check["status"] == "fail"
    assert check["repairable"] is False
    assert check["repaired"] is False
    assert str(ro) in check["detail"]
    assert "puffpai.fake.events" in check["detail"]
    assert "chmod" in check["suggestion"]


def test_missing_dir_under_writable_root_is_reported_repairable(
        tmp_path, monkeypatch):
    missing = tmp_path / "state"
    monkeypatch.setattr(self_check, "STATE_PATHS",
                        (_paths_entry("queue", missing),))
    check = _only(self_check.check_state_paths(), "state_path:queue")
    assert check["status"] == "fail"
    assert check["repairable"] is True
    assert check["repaired"] is False
    assert not missing.exists()


def test_repair_false_makes_no_filesystem_change(tmp_path, monkeypatch):
    missing = tmp_path / "state" / "deep"
    monkeypatch.setattr(self_check, "STATE_PATHS",
                        (_paths_entry("queue", missing),))
    before = _snapshot(tmp_path)
    self_check.check_state_paths(repair=False)
    self_check.check_state_paths(repair=False)
    assert _snapshot(tmp_path) == before


def test_repair_creates_the_directory_and_is_idempotent(tmp_path, monkeypatch):
    missing = tmp_path / "state"
    monkeypatch.setattr(self_check, "STATE_PATHS",
                        (_paths_entry("queue", missing),))

    first = _only(self_check.check_state_paths(repair=True), "state_path:queue")
    assert first["status"] == "ok"
    assert first["repaired"] is True
    assert missing.is_dir()

    # Idempotent: a second repair run changes nothing and claims nothing.
    marker = missing / "keep.txt"
    marker.write_text("state", encoding="utf-8")
    second = _only(self_check.check_state_paths(repair=True), "state_path:queue")
    assert second["status"] == "ok"
    assert second["repaired"] is False
    assert marker.read_text(encoding="utf-8") == "state"


def test_repair_refuses_to_create_a_deep_tree(tmp_path, monkeypatch):
    """A misconfigured absolute path must not conjure a directory tree."""
    deep = tmp_path / "a" / "b" / "c" / "d"
    monkeypatch.setattr(self_check, "STATE_PATHS", (_paths_entry("queue", deep),))
    check = _only(self_check.check_state_paths(repair=True), "state_path:queue")
    assert check["status"] == "fail"
    assert check["repaired"] is False
    assert not (tmp_path / "a").exists()


def test_repair_refuses_when_the_root_is_not_writable(tmp_path, monkeypatch):
    """Repair must never widen permissions to get itself a directory."""
    missing = tmp_path / "state"
    monkeypatch.setattr(self_check, "STATE_PATHS",
                        (_paths_entry("queue", missing),))
    monkeypatch.setattr(self_check, "_is_writable", lambda p: False)
    check = _only(self_check.check_state_paths(repair=True), "state_path:queue")
    assert check["status"] == "fail"
    assert check["repairable"] is False
    assert not missing.exists()


def test_a_broken_resolver_becomes_a_finding_not_an_exception(monkeypatch):
    def boom():
        raise RuntimeError("config exploded")

    monkeypatch.setattr(self_check, "STATE_PATHS",
                        (("events", "puffpai.fake", "the log", boom),))
    check = _only(self_check.check_state_paths(repair=True), "state_path:events")
    assert check["status"] == "warn"
    assert "config exploded" in check["detail"]


def test_the_real_registry_covers_every_module_that_bit_this_build():
    ids = {suffix for suffix, _, _, _ in self_check.STATE_PATHS}
    assert {"agent_events", "vault", "audit_log", "majordomo",
            "consent_gate", "consent_requests"} <= ids


def test_the_real_registry_resolvers_all_return_a_path():
    for suffix, module, _holds, resolver in self_check.STATE_PATHS:
        resolved = resolver()
        assert isinstance(resolved, str) and resolved, f"{module} ({suffix})"
        assert os.path.isabs(resolved), f"{module} resolved a relative path"


# ── 2. stale credential ─────────────────────────────────────────────

def test_ha_401_is_reported_never_rotated():
    bridge = StubBridge({"connected": False,
                         "error": "HA returned HTTP 401: Unauthorized"})
    check = _only(self_check.check_ha_credential(bridge), "ha_credential")
    assert check["status"] == "fail"
    assert check["repairable"] is False
    assert check["repaired"] is False
    assert "human must issue" in check["suggestion"]


def test_ha_unreachable_is_a_warning_not_a_credential_failure():
    bridge = StubBridge({"connected": False,
                         "error": "Cannot connect to Home Assistant at http://x"})
    check = _only(self_check.check_ha_credential(bridge), "ha_credential")
    assert check["status"] == "warn"


def test_ha_connected_is_ok():
    bridge = StubBridge({"connected": True, "ha_version": "2026.7",
                         "location_name": "Home"})
    assert _only(self_check.check_ha_credential(bridge),
                 "ha_credential")["status"] == "ok"


def test_ha_disabled_is_ok():
    bridge = StubBridge({"connected": False,
                         "error": "Home Assistant integration is disabled"})
    assert _only(self_check.check_ha_credential(bridge),
                 "ha_credential")["status"] == "ok"


@pytest.mark.parametrize("bridge", [
    StubBridge(raises=RuntimeError("boom")),
    StubBridge(status="not a dict"),
    StubBridge(status=None),
    None,
])
def test_ha_credential_never_raises(bridge):
    checks = self_check.check_ha_credential(bridge)
    assert checks[0]["status"] in ("ok", "warn", "fail")


# ── 3. vault uninitialized vs unwritable ────────────────────────────

@pytest.fixture
def vault_dir(tmp_path, monkeypatch):
    d = tmp_path / "vault_data"
    monkeypatch.setattr("puffpai.security.vault.default_vault_dir", lambda: d)
    return d


def test_uninitialized_vault_is_normal_and_needs_a_human(vault_dir):
    vault_dir.mkdir()
    check = _only(self_check.check_vault_initialized(), "vault_uninitialized")
    assert check["status"] == "warn"
    assert check["repairable"] is False
    assert "human" in check["detail"] or "human" in check["suggestion"]


def test_unwritable_vault_is_a_bug_not_a_missing_pin(vault_dir, monkeypatch):
    vault_dir.mkdir()
    monkeypatch.setattr(self_check, "_is_writable", lambda p: False)
    check = _only(self_check.check_vault_initialized(), "vault_uninitialized")
    assert check["status"] == "fail"
    assert "deployment bug" in check["detail"]


def test_initialized_vault_is_ok(vault_dir):
    vault_dir.mkdir()
    (vault_dir / ".vault_key_salt").write_bytes(b"x" * 64)
    assert _only(self_check.check_vault_initialized(),
                 "vault_uninitialized")["status"] == "ok"


def test_vault_check_never_reads_the_salt(vault_dir, monkeypatch):
    """The check must decide from presence alone — never open key material."""
    vault_dir.mkdir()
    (vault_dir / ".vault_key_salt").write_bytes(b"secret")
    real_open = open

    def guard(path, *a, **kw):
        assert ".vault_key_salt" not in str(path)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", guard)
    self_check.check_vault_initialized()


def test_vault_resolution_failure_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("no config")

    monkeypatch.setattr("puffpai.security.vault.default_vault_dir", boom)
    assert _only(self_check.check_vault_initialized(),
                 "vault_uninitialized")["status"] == "warn"


# ── 4. devices vanished from the network ────────────────────────────

def test_mass_unavailability_is_informational_and_grouped():
    """The real event: 128 entities went quiet at once on an ISP change."""
    states = [_entity(f"sensor.zigbee_{i}", "unavailable") for i in range(100)]
    states += [_entity(f"light.hue_{i}", "unavailable") for i in range(28)]
    states += [_entity(f"switch.local_{i}", "on") for i in range(82)]

    check = _only(self_check.check_ha_entities(StubBridge(states=states)),
                  "ha_entities_unavailable")
    assert check["status"] == "warn", "mass unavailability must never read as fail"
    assert check["status"] != "fail"
    assert check["data"]["unavailable"] == 128
    assert check["data"]["total_entities"] == 210
    assert check["data"]["by_domain"] == {"sensor": 100, "light": 28}
    assert "sensor x100" in check["detail"]
    assert "hub" in check["detail"]
    assert check["repairable"] is False


def test_all_entities_available_is_ok():
    states = [_entity(f"light.a_{i}", "on") for i in range(5)]
    assert _only(self_check.check_ha_entities(StubBridge(states=states)),
                 "ha_entities_unavailable")["status"] == "ok"


@pytest.mark.parametrize("bridge", [
    StubBridge(states="not a list"),
    StubBridge(states=[None, 3, {}, {"entity_id": ""}]),
    StubBridge(raises=RuntimeError("boom")),
    None,
])
def test_entity_check_never_raises(bridge):
    checks = self_check.check_ha_entities(bridge)
    assert checks[0]["status"] in ("ok", "warn")


# ── 5. an index shard left `building` after a crash ─────────────────

@pytest.fixture
def fts_dir(tmp_path, monkeypatch):
    d = tmp_path / "fts"
    d.mkdir()
    monkeypatch.setattr("puffpai.knowledge.fts_index.resolve_fts_dir",
                        lambda cfg=None: str(d))
    return d


def _stub_shard_status(monkeypatch, table):
    monkeypatch.setattr(
        "puffpai.knowledge.fts_index.shard_status",
        lambda db_path: table.get(os.path.basename(db_path), {"status": "complete"}))


def _age_files(paths, seconds):
    """Backdate every path's mtime by *seconds*."""
    old = time.time() - seconds
    for p in paths:
        os.utime(str(p), (old, old))


def _building_shard(fts_dir, monkeypatch, *, idle_seconds, name="wikipedia"):
    db = fts_dir / f"{name}.db"
    db.write_bytes(b"")
    _stub_shard_status(monkeypatch, {
        f"{name}.db": {"status": "building", "last_entry_index": 41000,
                       "article_count": 900},
    })
    _age_files([db], idle_seconds)
    return db


# A build in progress carries the identical `building` flag as a crashed one;
# only recency of progress separates them.
@pytest.mark.parametrize("idle_seconds,expected", [
    (0, "ok"),                                                   # writing now
    (30, "ok"),                                                  # between batches
    (self_check.SHARD_PROGRESS_STALE_SECONDS - 60, "ok"),        # slow, alive
    (self_check.SHARD_PROGRESS_STALE_SECONDS + 60, "warn"),      # crashed
    (6 * 3600, "warn"),                                          # long dead
])
def test_building_shard_is_judged_by_progress_recency(
        fts_dir, monkeypatch, idle_seconds, expected):
    _building_shard(fts_dir, monkeypatch, idle_seconds=idle_seconds)
    check = _only(self_check.check_index_shards(), "fts_shards")
    assert check["status"] == expected


def test_a_live_build_reports_as_active_not_as_a_fault(fts_dir, monkeypatch):
    """The reproduced false positive: a shard being built right now."""
    _building_shard(fts_dir, monkeypatch, idle_seconds=0)
    check = _only(self_check.check_index_shards(), "fts_shards")
    assert check["status"] == "ok"
    assert "building (active" in check["detail"]
    assert "not a fault" in check["detail"]
    assert "stale" not in check["detail"]
    assert [s["library_id"] for s in check["data"]["active"]] == ["wikipedia"]
    assert check["data"]["stale"] == []


def test_stale_shard_is_reported_but_never_rebuilt_here(fts_dir, monkeypatch):
    (fts_dir / "wiktionary.db").write_bytes(b"")
    _building_shard(fts_dir, monkeypatch, idle_seconds=3 * 3600)
    before = _snapshot(fts_dir)

    check = _only(self_check.check_index_shards(), "fts_shards")
    assert check["status"] == "warn"
    assert check["repairable"] is False
    assert check["repaired"] is False
    assert "stale mid-build" in check["detail"]
    assert "no progress for 180m" in check["detail"]
    assert "resumable" in check["detail"]
    assert "dispatcher" in check["suggestion"]
    assert [s["library_id"] for s in check["data"]["stale"]] == ["wikipedia"]
    assert [s["library_id"] for s in check["data"]["building"]] == ["wikipedia"]
    assert _snapshot(fts_dir) == before, "the shard check must not touch the index"


def test_progress_is_read_from_the_wal_sidecar_too(fts_dir, monkeypatch):
    """WAL mode means batch commits land in -wal; the .db can lag behind."""
    db = _building_shard(fts_dir, monkeypatch, idle_seconds=6 * 3600)
    (fts_dir / "wikipedia.db-wal").write_bytes(b"")   # committed a moment ago
    check = _only(self_check.check_index_shards(), "fts_shards")
    assert check["status"] == "ok", "a fresh -wal proves the build is alive"
    assert db.exists()


def test_an_active_and_a_stale_shard_are_reported_separately(
        fts_dir, monkeypatch):
    live = fts_dir / "wiktionary.db"
    dead = fts_dir / "wikipedia.db"
    live.write_bytes(b"")
    dead.write_bytes(b"")
    _stub_shard_status(monkeypatch, {
        "wiktionary.db": {"status": "building"},
        "wikipedia.db": {"status": "building"},
    })
    _age_files([dead], 2 * 3600)

    check = _only(self_check.check_index_shards(), "fts_shards")
    assert check["status"] == "warn"
    assert [s["library_id"] for s in check["data"]["stale"]] == ["wikipedia"]
    assert [s["library_id"] for s in check["data"]["active"]] == ["wiktionary"]
    assert "building normally" in check["detail"]


def test_a_shard_that_vanishes_between_listing_and_stat_never_raises(
        fts_dir, monkeypatch):
    db = fts_dir / "wikipedia.db"
    db.write_bytes(b"")

    def vanishing(db_path):
        os.remove(db_path)                      # gone before the mtime stat
        return {"status": "building"}

    monkeypatch.setattr("puffpai.knowledge.fts_index.shard_status", vanishing)
    check = _only(self_check.check_index_shards(), "fts_shards")
    # No evidence of stillness -> not a warning. Never cry wolf on no data.
    assert check["status"] == "ok"
    assert check["data"]["active"][0]["idle_seconds"] is None


def test_complete_shards_are_ok(fts_dir, monkeypatch):
    (fts_dir / "wikipedia.db").write_bytes(b"")
    _stub_shard_status(monkeypatch, {})
    assert _only(self_check.check_index_shards(), "fts_shards")["status"] == "ok"


def test_missing_index_dir_is_ok(tmp_path, monkeypatch):
    monkeypatch.setattr("puffpai.knowledge.fts_index.resolve_fts_dir",
                        lambda cfg=None: str(tmp_path / "absent"))
    assert _only(self_check.check_index_shards(), "fts_shards")["status"] == "ok"


def test_shard_status_blowing_up_never_raises(fts_dir, monkeypatch):
    (fts_dir / "wikipedia.db").write_bytes(b"")

    def boom(db_path):
        raise RuntimeError("sqlite is on fire")

    monkeypatch.setattr("puffpai.knowledge.fts_index.shard_status", boom)
    check = _only(self_check.check_index_shards(), "fts_shards")
    assert check["status"] == "warn"
    assert len(check["data"]["errored"]) == 1


# ── run_self_check() as a whole ─────────────────────────────────────

def test_run_self_check_shape_and_no_network_by_default(tmp_path, monkeypatch):
    good = tmp_path / "state"
    good.mkdir()
    monkeypatch.setattr(self_check, "STATE_PATHS", (_paths_entry("events", good),))
    monkeypatch.setattr(self_check, "_default_bridge",
                        lambda: pytest.fail("must not build a live bridge"))

    result = self_check.run_self_check(probe_network=False)
    assert set(result) == {"checks", "summary"}
    for c in result["checks"]:
        assert set(c) == {"id", "title", "status", "detail", "repairable",
                          "repaired", "suggestion", "data"}
        assert c["status"] in ("ok", "warn", "fail")
    summary = result["summary"]
    assert summary["total"] == len(result["checks"])
    assert summary["repair_enabled"] is False
    assert summary["status"] in ("ok", "warn", "fail")


def test_run_self_check_without_repair_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(self_check, "STATE_PATHS",
                        (_paths_entry("events", tmp_path / "missing"),))
    monkeypatch.setattr("puffpai.security.vault.default_vault_dir",
                        lambda: tmp_path / "vault_data")
    monkeypatch.setattr("puffpai.knowledge.fts_index.resolve_fts_dir",
                        lambda cfg=None: str(tmp_path / "fts"))
    before = _snapshot(tmp_path)
    self_check.run_self_check(probe_network=False)
    assert _snapshot(tmp_path) == before


def test_run_self_check_survives_a_detector_that_explodes(monkeypatch):
    monkeypatch.setattr(self_check, "check_index_shards",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    result = self_check.run_self_check(probe_network=False)
    assert "fts_shards" not in [c["id"] for c in result["checks"]]
    assert result["summary"]["total"] == len(result["checks"])


# ── Concierge decider ───────────────────────────────────────────────

def _event(data: dict) -> Envelope:
    return Envelope(
        intent="event",
        payload={"source": "self_check", "event_type": "self_check",
                 "data": data},
        requester="system",
        origin_agent="scheduler",
        budget=Budget(max_actions=2, max_seconds=15.0, allow_egress=False),
        state_changing=False,
    )


def _decide(data: dict) -> dict:
    reply = ConciergeAgent().handle(_event(data))
    assert reply["ok"] is True
    assert reply["event_type"] == "self_check"
    return reply["decision"]


def _findings(*specs) -> dict:
    return {"checks": [{"id": i, "status": s, "title": i, "detail": f"{i} said so",
                        "suggestion": "", "repairable": False, "repaired": False}
                       for i, s in specs]}


CLASSIFICATION = [
    # findings                                              severity     action
    ([("state_path:agent_events", "ok")],                   "info",      "logged"),
    ([("state_path:agent_events", "fail")],                 "attention", "needs_human"),
    ([("state_path:vault", "fail"),
      ("ha_credential", "fail")],                           "attention", "needs_human"),
    ([("ha_credential", "fail")],                           "attention", "needs_human"),
    ([("fts_shards", "warn")],                              "info",      "needs_human"),
    # informational-only: loud, but a fact about the house, never attention
    ([("ha_entities_unavailable", "fail")],                 "info",      "needs_human"),
    ([("vault_uninitialized", "warn")],                     "info",      "needs_human"),
    ([("ha_entities_unavailable", "fail"),
      ("vault_uninitialized", "warn")],                     "info",      "needs_human"),
    # an informational failure alongside a real one still escalates
    ([("ha_entities_unavailable", "fail"),
      ("state_path:vault", "fail")],                        "attention", "needs_human"),
]


@pytest.mark.parametrize("specs,severity,action", CLASSIFICATION)
def test_self_check_classification_table(specs, severity, action):
    decision = _decide(_findings(*specs))
    assert decision["severity"] == severity
    assert decision["suggested_action"] == action


def test_decision_is_always_suggestion_only():
    decision = _decide(_findings(("state_path:vault", "fail")))
    assert decision["suggestion_only"] is True
    assert decision["action_taken"] is None
    assert "does not repair state" in decision["note"]


def test_informational_ids_match_the_module_contract():
    assert SELF_CHECK_INFORMATIONAL == frozenset(
        {"ha_entities_unavailable", "vault_uninitialized"})


def test_all_clear_reports_logged():
    decision = _decide(_findings(("state_path:vault", "ok"),
                                 ("ha_credential", "ok")))
    assert decision["severity"] == "info"
    assert decision["suggested_action"] == "logged"
    assert "passed all 2 checks" in decision["summary"]


def test_repairs_are_named_in_the_summary():
    data = {"checks": [{"id": "state_path:queue", "status": "ok",
                        "repaired": True, "detail": "created"}]}
    decision = _decide(data)
    assert decision["repaired"] == ["state_path:queue"]
    assert "state_path:queue" in decision["summary"]


def test_nested_self_check_payload_is_accepted():
    inner = _findings(("state_path:vault", "fail"))
    decision = _decide({"self_check": inner})
    assert decision["severity"] == "attention"


# ── dedup (the same mechanism service_degraded established) ──────────

def test_identical_findings_are_deduplicated(clock):
    data = _findings(("state_path:vault", "fail"))
    first = _decide(data)
    assert first["duplicate"] is False
    assert first["severity"] == "attention"

    clock["t"] += 60
    second = _decide(data)
    assert second["duplicate"] is True
    assert second["severity"] == "info"
    assert second["suggested_action"] == "logged"
    assert second["escalated_action"] == "needs_human"
    assert "already reported" in second["summary"]


def test_dedup_expires(clock):
    data = _findings(("state_path:vault", "fail"))
    _decide(data)
    clock["t"] += SERVICE_DEGRADED_DEDUP_SECONDS + 1
    assert _decide(data)["duplicate"] is False


def test_a_changed_finding_always_reports(clock):
    _decide(_findings(("state_path:vault", "fail")))
    clock["t"] += 60
    escalated = _decide(_findings(("state_path:vault", "fail"),
                                  ("ha_credential", "fail")))
    assert escalated["duplicate"] is False
    assert escalated["severity"] == "attention"


def test_recovery_forgets_only_self_check_fingerprints(clock):
    """A clean self-check must not erase what service_degraded remembers."""
    degraded = Envelope(
        intent="event",
        payload={"source": "nodered", "event_type": "service_degraded",
                 "data": {"down_services": ["PuffPAI Dashboard"], "down_count": 1}},
        requester="system", origin_agent="nodered",
        budget=Budget(max_actions=2, max_seconds=15.0, allow_egress=False),
        state_changing=False,
    )
    agent = ConciergeAgent()
    assert agent.handle(degraded)["decision"]["duplicate"] is False

    _decide(_findings(("state_path:vault", "fail")))
    clock["t"] += 60
    _decide(_findings(("state_path:vault", "ok")))          # all clear
    clock["t"] += 60

    # service_degraded's memory survived...
    assert agent.handle(degraded)["decision"]["duplicate"] is True
    # ...and the self-check fault reports fresh again.
    assert _decide(_findings(("state_path:vault", "fail")))["duplicate"] is False


# ── malformed input never raises ────────────────────────────────────

@pytest.mark.parametrize("data", [
    {},
    {"checks": None},
    {"checks": "not a list"},
    {"checks": [None, 5, "x"]},
    {"checks": [{}]},
    {"checks": [{"id": None, "status": None}]},
    {"checks": [{"id": "x" * 5000, "status": "banana",
                 "detail": "y" * 9000, "suggestion": "z" * 9000}]},
    {"checks": [{"id": f"c{i}", "status": "fail"} for i in range(500)]},
    {"self_check": "not a dict"},
    {"self_check": {"checks": []}},
])
def test_malformed_self_check_payloads_never_raise(data):
    decision = _decide(data)
    assert decision["severity"] in ("info", "attention")
    assert decision["suggested_action"] in ("logged", "needs_human")
    assert isinstance(decision["summary"], str)
    assert len(decision["summary"]) < 2000
