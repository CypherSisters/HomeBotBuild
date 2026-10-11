"""The Agent Team activity feed: one resolver for its reader and its writers.

``agent-activity.jsonl`` must not have two independent answers for
where it lives. ``api_server`` -- the feed's only READER -- resolves it through
``resolve_state_dir``; if ``security/consent_requests`` instead walked
directory levels up into a sibling checkout, the write path would point
somewhere that does not exist and cannot be created under
``ProtectSystem=strict``, so every append would raise and
``except OSError: pass`` would drop it: the record of "Sentinel
asked the user to approve device X" and "the user granted/denied it" would go
missing from the one place it matters, with nothing logged.

These cases pin the three properties that fix requires: the feed resolves like
every other piece of runtime state, its writers land where its reader looks,
and a feed that cannot be written says so.
"""

from __future__ import annotations

import json
import logging
import os
import types

import pytest

import puffpai.agent_activity as aa
from puffpai.logging_config import reset_unwritable_reports

ACTIVITY = aa.ACTIVITY_LOG_NAME


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    """Resolution is cached per process; every case starts from a clean one."""
    aa.reset_cache()
    reset_unwritable_reports()
    yield
    aa.reset_cache()
    reset_unwritable_reports()


def _no_config(monkeypatch):
    """Make puffpai.config unimportable so the statedir leaf answers.

    That is the degraded tier, and it is the one that must still honour
    $PUFFPAI_DATA_DIR -- reading the env var raw (no strip, no ~, no repo-root
    join) is exactly how a module ends up writing outside the writable root.
    """
    import builtins
    real_import = builtins.__import__

    def fake(name, *args, **kwargs):
        if name == "puffpai.config":
            raise ImportError("config unavailable (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)


def _stub_config(monkeypatch, *, agent_logs_dir="", state_dir="", repo_root=None):
    """Stand in for get_config() with only the fields the resolver reads."""
    import puffpai.config as cfg_mod
    cfg = types.SimpleNamespace(
        paths=types.SimpleNamespace(
            agent_logs_dir=agent_logs_dir, state_dir=state_dir),
        REPO_ROOT=repo_root or aa._REPO_ROOT,
    )
    monkeypatch.setattr(cfg_mod, "get_config", lambda: cfg)


# ── where the feed lands ────────────────────────────────────────────

def test_env_var_moves_the_feed(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))

    assert aa.get_activity_log_path() == os.path.join(
        str(tmp_path / "state"), ACTIVITY)


def test_a_relative_env_value_joins_the_repo_root_not_the_cwd(monkeypatch):
    """A relative $PUFFPAI_DATA_DIR taken against the CWD lands wherever the
    process happened to start -- the canon joins it to the repo root."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", "relstate")

    assert aa.get_activity_log_path() == os.path.join(
        aa._REPO_ROOT, "relstate", ACTIVITY)


def test_the_env_unset_default_is_the_shared_data_dir(monkeypatch):
    """<repo>/data -- beside agent_events.jsonl and the consent ledger, not in
    another project's tree."""
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    _no_config(monkeypatch)

    assert aa.get_activity_log_path() == os.path.join(
        aa._DEFAULT_DATA_DIR, ACTIVITY)


def test_configured_state_dir_wins_when_the_env_var_is_unset(tmp_path, monkeypatch):
    """The Orin's own shape: paths.state_dir absolute, no env var.

    This is the configuration the old split was invisible in -- the reader
    honoured paths.state_dir and the consent loop did not, so they would have
    written and read two different files even after the sibling path was gone.
    """
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    _stub_config(monkeypatch, state_dir=str(tmp_path / "appliance"))

    assert aa.get_activity_log_path() == os.path.join(
        str(tmp_path / "appliance"), ACTIVITY)


def test_agent_logs_dir_overrides_state_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    _stub_config(monkeypatch,
                 agent_logs_dir=str(tmp_path / "logs"),
                 state_dir=str(tmp_path / "state"))

    assert aa.agent_logs_dir() == str(tmp_path / "logs")


def test_agent_logs_dir_is_a_real_config_key():
    """api_server read paths.agent_logs_dir through a getattr default for
    months; the resolver reads it directly, so the field must exist."""
    from puffpai.config import PathConfig

    assert "agent_logs_dir" in PathConfig.__dataclass_fields__
    assert PathConfig().agent_logs_dir == ""


# ── other agent logs ────────────────────────────────────────────────

def test_the_task_queue_resolves_the_same_way(tmp_path, monkeypatch):
    """task-queue.jsonl lives beside the feed, in the same directory."""
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    _no_config(monkeypatch)
    monkeypatch.setattr(aa, "_DEFAULT_DATA_DIR", str(tmp_path / "new"))

    assert aa.resolve_agent_log("task-queue.jsonl") == os.path.join(
        str(tmp_path / "new"), "task-queue.jsonl")
    assert aa.get_activity_log_path() == os.path.join(str(tmp_path / "new"), ACTIVITY)


# ── writing: never fatal, never silent ──────────────────────────────

def test_log_activity_writes_the_schema_its_readers_expect(tmp_path, monkeypatch):
    """/agents/status groups by "agent" and ages out non-terminal "status"."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))

    assert aa.log_activity("sentinel", "consent_requested",
                           "Consent request: unlock for Front lock",
                           "waiting_user", {"entity_id": "lock.front"})

    entry = json.loads((tmp_path / ACTIVITY).read_text(encoding="utf-8"))
    assert entry["agent"] == "sentinel"
    assert entry["event"] == "consent_requested"
    assert entry["status"] == "waiting_user"
    assert entry["result"] == {"entity_id": "lock.front"}
    assert entry["timestamp"].endswith("+00:00")


def test_an_unwritable_feed_is_reported_once_and_never_raises(
        tmp_path, monkeypatch, caplog):
    """The regression this whole change exists to end.

    A path the process cannot write is a deployment fault that will not heal,
    and this feed is the household's only view of the consent conversation.
    Losing it must not break the user path -- and must not be silent either.
    """
    dead = tmp_path / "read-only" / ACTIVITY

    def boom(*args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(aa.os, "makedirs", boom)

    with caplog.at_level(logging.ERROR):
        assert aa.append({"agent": "sentinel"}, path=str(dead)) is False
        assert aa.append({"agent": "sentinel"}, path=str(dead)) is False

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, "one report per process, not one per entry"
    assert str(dead) in errors[0].getMessage()
    assert "ReadWritePaths" in errors[0].getMessage()


def test_an_unserializable_entry_is_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))

    assert aa.log_activity("sentinel", "e", "t", "s", {"obj": object()}) is True
    assert "object at 0x" in (tmp_path / ACTIVITY).read_text(encoding="utf-8")


# ── the consent loop actually reaches the feed ──────────────────────

def _reload_requests(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    import puffpai.security.consent_requests as cr
    importlib.reload(cr)
    aa.reset_cache()
    return cr


def test_a_consent_request_and_its_verdict_reach_the_feed(tmp_path, monkeypatch):
    """The end-to-end point: both halves of the ask now show up in the feed.

    On the appliance neither line was written at all -- the request went to the
    user and the answer went to the ConsentStore, while the feed that is
    supposed to show both stayed empty.
    """
    cr = _reload_requests(tmp_path, monkeypatch)
    import puffpai.security.consent_gate as cg
    monkeypatch.setattr(cg, "CONSENT_PATH", str(tmp_path / "device_consent.json"),
                        raising=False)
    monkeypatch.setattr(cg, "_store", None, raising=False)

    cr.create_request("lock.front", "Front lock", "unlock", "asked in chat")
    cr.resolve("lock.front", approved=True, by="user_via_chat")

    lines = [json.loads(line) for line
             in (tmp_path / ACTIVITY).read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in lines] == ["consent_requested", "consent_resolved"]
    assert all(e["agent"] == "sentinel" for e in lines)
    assert lines[0]["status"] == "waiting_user"
    assert lines[1]["result"] == {"entity_id": "lock.front", "approved": True,
                                  "by": "user_via_chat"}


def test_the_consent_loop_and_the_feed_agree_on_one_path(tmp_path, monkeypatch):
    """A writer that resolves separately from the reader is the same outage in
    a different directory."""
    cr = _reload_requests(tmp_path, monkeypatch)

    assert cr.ACTIVITY_LOG == aa.get_activity_log_path()
    assert cr._activity_log_path() == aa.get_activity_log_path()


def test_an_explicit_activity_log_override_still_wins(tmp_path, monkeypatch):
    """The test/ops override hook, checked before the shared resolver."""
    cr = _reload_requests(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere" / "activity.jsonl"
    elsewhere.parent.mkdir()
    monkeypatch.setattr(cr, "ACTIVITY_LOG", str(elsewhere))

    cr.log_agent_activity("sentinel", "consent_requested", "t", "waiting_user")

    assert elsewhere.is_file()
    assert not (tmp_path / ACTIVITY).exists()
