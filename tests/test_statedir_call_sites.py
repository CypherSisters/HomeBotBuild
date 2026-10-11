"""The state-dir call sites migrated onto ``puffpai.statedir``.

Companion to ``tests/test_statedir.py`` (which pins the leaf itself against the
canon) and ``tests/test_zone3_paths.py`` (same shape, one module). This file
covers the CALLERS that use ``puffpai.statedir``:

* ``agents/caretaker.py``   — the watchdog restart ledger
* ``agents/toolsmith.py``   — the sandbox workspace root
* ``device_sentinel.py``    — sentinel baseline + alert trail
* ``api_server.py``         — only that it resolves the agent logs LAZILY; the
  resolution itself moved to :mod:`puffpai.agent_activity` and is covered by
  ``tests/test_agent_activity.py``, which is not duplicated here.

Two of them read ``$PUFFPAI_DATA_DIR`` RAW in their "config unavailable"
fallback — no strip, no ``~`` expansion, no repo-root join for a relative value
— so they DRIFTED from the canon rather than merely duplicating it. That branch
runs only when ``get_config()`` raises, which is why nearly every case here
installs :func:`broken_config`: testing the happy path would have left the bug
untouched. The third (device_sentinel) was a faithful replica, and the cases
below are what keep the swap honest.

Legacy adoption is opt-in per case, never ambient. This checkout really holds
files under ``src/puffpai/data``, so a case that exercised the env-unset
default with a module's legacy attribute still pointing there would silently
depend on real state in the working tree — and would flip its answer the day
someone deleted that file. The autouse fixture repoints every legacy attribute
at an absent tmp directory; the adoption cases repoint it at one they seeded
themselves.
"""

from __future__ import annotations

import json
import os

import pytest

from puffpai.statedir import _REPO_ROOT

import puffpai.agents.caretaker as caretaker
import puffpai.agents.toolsmith as toolsmith
import puffpai.device_sentinel as device_sentinel


# ── shared fixtures ─────────────────────────────────────────────────

@pytest.fixture
def broken_config(monkeypatch):
    """Force the degraded tier: ``get_config()`` raises, the leaf answers.

    This is the branch that used to read the env var raw. Note only
    ``get_config`` is broken — ``resolve_state_dir`` stays real, because the
    leaf reaches the canon on its own and that is precisely the property under
    test.
    """
    import puffpai.config as config_mod

    def _boom():
        raise RuntimeError("config unavailable (test)")

    monkeypatch.setattr(config_mod, "get_config", _boom)


@pytest.fixture(autouse=True)
def _neutralized_legacy(monkeypatch, tmp_path):
    """Neutralise every module's legacy location and drop every cached path."""
    absent = str(tmp_path / "absent_legacy")
    monkeypatch.setattr(caretaker, "_LEGACY_DATA_DIR", absent)
    monkeypatch.setattr(caretaker, "RESTART_LEDGER_PATH", None)
    monkeypatch.setattr(toolsmith, "_LEGACY_DATA_DIR", absent)
    monkeypatch.setattr(device_sentinel, "_data_dir_cache", None)
    # Back to None, the unset state: test_sentinel assigns these directly and
    # never restores them, so a path it pinned would otherwise decide the
    # answer for every case here.
    for name in ("_DATA_DIR", "_BASELINE_FILE", "_ALERTS_FILE"):
        monkeypatch.setattr(device_sentinel, name, None)


@pytest.fixture
def api_server_mod(monkeypatch):
    """``api_server`` with the shared agent-log path cache dropped.

    Imported inside the fixture, not at module scope: importing api_server has
    real side effects (the API-key path), and the session-scoped secret-hygiene
    guard in conftest is what makes that safe.

    ``puffpai.agent_activity`` is importorskip'd rather than imported: it landed
    in a parallel change that moved the agent-log resolution out of api_server,
    and these cases are only meaningful once it exists. Where it is absent, so
    is the lazy accessor pair they check, and skipping is the honest answer.
    """
    agent_activity = pytest.importorskip("puffpai.agent_activity")
    import puffpai.api_server as api_server

    # The cache is shared process-wide and keyed by filename, so it is dropped
    # on the way IN and on the way OUT: a path resolved under this test's
    # environment must not outlive the test.
    agent_activity.reset_cache()
    try:
        yield api_server
    finally:
        agent_activity.reset_cache()


#: Every migrated resolver, as (id, callable returning an absolute path).
#: Parametrising the env-handling cases is the point: these drifted from the
#: canon and from each other precisely because each was checked (or not) on
#: its own.
RESOLVERS = {
    "caretaker": lambda: os.path.dirname(caretaker._resolve_ledger_path()),
    "toolsmith": lambda: os.path.dirname(toolsmith._resolve_workspace_root()),
    "device_sentinel": lambda: str(device_sentinel._resolve_data_dir()),
}

ALL_MODULES = list(RESOLVERS)


@pytest.fixture
def data_dir_of(monkeypatch):
    """Return ``fn(module_name) -> resolved data directory`` for any call site."""

    def _get(name: str) -> str:
        monkeypatch.setattr(caretaker, "RESTART_LEDGER_PATH", None)
        monkeypatch.setattr(device_sentinel, "_data_dir_cache", None)
        return os.path.realpath(RESOLVERS[name]())

    return _get


# ── the env contract, held identically by all four ──────────────────

@pytest.mark.parametrize("module", ALL_MODULES)
class TestEnvHandlingMatchesCanon:
    """$PUFFPAI_DATA_DIR means the same thing at every call site."""

    def test_relative_value_joins_the_repo_root_not_the_cwd(
        self, module, data_dir_of, broken_config, monkeypatch, tmp_path,
    ):
        """The drift that mattered: a relative value is repo-relative.

        ``"data"`` is the documented relative value, and the old raw read took
        it verbatim — i.e. relative to whatever directory the service happened
        to start in. The chdir below is what makes the difference visible; a
        raw read answers ``<cwd>/relstate`` here.
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "relstate")
        assert data_dir_of(module) == os.path.realpath(
            os.path.join(_REPO_ROOT, "relstate"))

    def test_absolute_value_is_honoured(
        self, module, data_dir_of, broken_config, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        assert data_dir_of(module) == os.path.realpath(str(tmp_path / "state"))

    def test_tilde_value_is_expanded(
        self, module, data_dir_of, broken_config, monkeypatch,
    ):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "~/puff_state")
        resolved = data_dir_of(module)
        assert "~" not in resolved
        assert resolved == os.path.realpath(
            os.path.expanduser("~/puff_state"))

    def test_untrimmed_value_is_stripped(
        self, module, data_dir_of, broken_config, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", f"  {tmp_path / 'state'}  ")
        assert data_dir_of(module) == os.path.realpath(str(tmp_path / "state"))

    def test_whitespace_only_value_reads_as_unset(
        self, module, data_dir_of, broken_config, monkeypatch,
    ):
        """Whitespace is "unset", so the shipped default answers — never ``""``
        (which would resolve to the CWD)."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "   ")
        assert data_dir_of(module) == os.path.realpath(
            os.path.join(_REPO_ROOT, "data"))

    def test_unset_env_lands_in_the_shared_repo_data_dir(
        self, module, data_dir_of, broken_config, monkeypatch,
    ):
        """All four converge on <repo>/data — the point of the migration.

        caretaker, toolsmith and api_server each shipped a different default
        (``src/puffpai/data`` twice, and a directory outside the repo).
        Scattered runtime state is how one
        module writes a trail another module never finds.
        """
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        assert data_dir_of(module) == os.path.realpath(
            os.path.join(_REPO_ROOT, "data"))


@pytest.mark.parametrize("module", ALL_MODULES)
def test_resolution_is_lazy_not_an_import_time_snapshot(
    module, data_dir_of, broken_config, monkeypatch, tmp_path,
):
    """The env set AFTER import still decides. A snapshot could not do this."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "late"))
    assert data_dir_of(module) == os.path.realpath(str(tmp_path / "late"))


class TestApiServerResolvesTheAgentLogsLazily:
    """The import-time landmine. Resolution itself lives in agent_activity now
    (see tests/test_agent_activity.py); what is checked here is only that
    api_server asks for it at USE time.

    ``_ACTIVITY_LOG = _agent_activity.get_activity_log_path()`` ran at MODULE
    SCOPE. agent_activity is a leaf and safe to import, but the resolution it
    performs reaches puffpai.config, and with it the secrets provider and the
    vault — the cycle risk puffpai.statedir exists to avoid.
    """

    def test_the_module_scope_constants_are_gone(self, api_server_mod):
        for gone in ("_AGENT_LOGS_DIR", "_ACTIVITY_LOG", "_TASK_QUEUE"):
            assert not hasattr(api_server_mod, gone), (
                f"{gone} is back at module scope; it resolves at import time")

    def test_the_accessors_delegate_to_the_feed_owner(self, api_server_mod):
        import puffpai.agent_activity as agent_activity

        assert api_server_mod._activity_log() == (
            agent_activity.get_activity_log_path())
        assert api_server_mod._task_queue() == (
            agent_activity.resolve_agent_log("task-queue.jsonl"))

    def test_an_env_change_after_import_still_moves_the_feed(
        self, api_server_mod, broken_config, monkeypatch, tmp_path,
    ):
        """A module-scope snapshot could not do this — it froze the answer
        before any test (or late environment edit) could move it."""
        import puffpai.agent_activity as agent_activity

        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "late"))
        agent_activity.reset_cache()
        try:
            assert api_server_mod._activity_log() == os.path.join(
                str(tmp_path / "late"), agent_activity.ACTIVITY_LOG_NAME)
        finally:
            agent_activity.reset_cache()


# ── file-level legacy adoption ──────────────────────────────────────
#
# Directory-level `legacy=` is useless for these: <repo>/data ships in every
# checkout, so the probe never fires. Each case below seeds the OLD item and
# points the module's default at an empty tmp dir, so adoption is the only
# thing that can produce the old path.

class TestCaretakerLedgerAdoption:
    @pytest.fixture
    def seeded(self, monkeypatch, tmp_path):
        legacy_dir = tmp_path / "legacy"
        legacy_dir.mkdir()
        ledger = legacy_dir / caretaker._LEDGER_FILENAME
        ledger.write_text(json.dumps({"restarts": [{"unit": "old"}]}),
                          encoding="utf-8")
        monkeypatch.setattr(caretaker, "_LEGACY_DATA_DIR", str(legacy_dir))
        monkeypatch.setattr(caretaker, "_DEFAULT_DATA_DIR",
                            str(tmp_path / "new"))
        monkeypatch.setattr(caretaker, "RESTART_LEDGER_PATH", None)
        return ledger

    def test_an_old_ledger_is_adopted_in_place(
        self, seeded, broken_config, monkeypatch,
    ):
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        assert caretaker._resolve_ledger_path() == str(seeded)

    def test_adoption_only_reads_in_place_and_never_copies(
        self, seeded, broken_config, monkeypatch, tmp_path,
    ):
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        caretaker._resolve_ledger_path()
        assert seeded.exists(), "the legacy ledger was moved"
        assert not (tmp_path / "new").exists(), (
            "adoption created/copied into the new location")

    def test_an_explicit_env_var_suppresses_adoption(
        self, seeded, broken_config, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "elsewhere"))
        assert caretaker._resolve_ledger_path() == os.path.join(
            str(tmp_path / "elsewhere"), caretaker._LEDGER_FILENAME)

    def test_env_pointed_at_the_default_still_suppresses_adoption(
        self, seeded, broken_config, monkeypatch, tmp_path,
    ):
        """The test is "is the var SET", not "did the
        resolved path move". Pointing the env at the default resolves to the
        default — a path comparison would read that as "unset" and adopt,
        writing the ledger into the read-only code tree with an env var
        explicitly set."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "new"))
        assert caretaker._resolve_ledger_path() == os.path.join(
            str(tmp_path / "new"), caretaker._LEDGER_FILENAME)

    def test_a_ledger_at_the_new_path_wins(
        self, seeded, broken_config, monkeypatch, tmp_path,
    ):
        new_dir = tmp_path / "new"
        new_dir.mkdir()
        (new_dir / caretaker._LEDGER_FILENAME).write_text("{}", encoding="utf-8")
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        assert caretaker._resolve_ledger_path() == os.path.join(
            str(new_dir), caretaker._LEDGER_FILENAME)

    def test_a_configured_state_dir_suppresses_adoption(
        self, seeded, monkeypatch, tmp_path,
    ):
        """A non-empty paths.state_dir is an explicit decision, like the env
        var — so this case does NOT install broken_config."""
        import puffpai.config as config_mod

        class _Cfg:
            REPO_ROOT = str(tmp_path / "repo")

            class paths:
                state_dir = str(tmp_path / "configured")

        monkeypatch.setattr(config_mod, "get_config", lambda: _Cfg())
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        assert caretaker._resolve_ledger_path() == os.path.join(
            str(tmp_path / "configured"), caretaker._LEDGER_FILENAME)


class TestToolsmithWorkspaceAdoption:
    @pytest.fixture
    def seeded(self, monkeypatch, tmp_path):
        legacy_ws = tmp_path / "legacy" / toolsmith.WORKSPACE_DIRNAME
        legacy_ws.mkdir(parents=True)
        (legacy_ws / "notes.txt").write_text("user file", encoding="utf-8")
        monkeypatch.setattr(toolsmith, "_LEGACY_DATA_DIR",
                            str(tmp_path / "legacy"))
        monkeypatch.setattr(toolsmith, "_DEFAULT_DATA_DIR",
                            str(tmp_path / "new"))
        return legacy_ws

    def test_an_old_workspace_is_adopted_in_place(
        self, seeded, broken_config, monkeypatch,
    ):
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        assert toolsmith._resolve_workspace_root() == os.path.realpath(
            str(seeded))

    def test_adoption_never_moves_the_users_files(
        self, seeded, broken_config, monkeypatch,
    ):
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        toolsmith._resolve_workspace_root()
        assert (seeded / "notes.txt").read_text(encoding="utf-8") == "user file"

    def test_an_explicit_env_var_suppresses_adoption(
        self, seeded, broken_config, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "elsewhere"))
        assert toolsmith._resolve_workspace_root() == os.path.realpath(
            os.path.join(str(tmp_path / "elsewhere"),
                         toolsmith.WORKSPACE_DIRNAME))

    def test_env_pointed_at_the_default_still_suppresses_adoption(
        self, seeded, broken_config, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "new"))
        assert toolsmith._resolve_workspace_root() == os.path.realpath(
            os.path.join(str(tmp_path / "new"), toolsmith.WORKSPACE_DIRNAME))

    def test_the_workspace_root_is_always_absolute(
        self, broken_config, monkeypatch, tmp_path,
    ):
        """Containment in ``_safe_path`` is a realpath-prefix proof against
        this root, so a relative root would not merely mis-locate the sandbox."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "relstate")
        assert os.path.isabs(toolsmith._resolve_workspace_root())

    def test_a_relative_env_value_does_not_strand_a_sandbox_in_the_cwd(
        self, broken_config, monkeypatch, tmp_path,
    ):
        """``workspace_root()`` os.makedirs its answer, so the old raw read did
        not merely mis-resolve — it CREATED a second sandbox under whatever
        directory the service started in."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "relstate")
        root = toolsmith._resolve_workspace_root()
        assert root.startswith(os.path.realpath(_REPO_ROOT) + os.sep)
        assert not (tmp_path / "relstate").exists()


# ── device_sentinel: the override contract the suites depend on ─────

class TestDeviceSentinelOverrides:
    """test_sentinel assigns these names directly; test_agent_events
    monkeypatches _ALERTS_FILE. Making the paths lazy must not break either."""

    def test_assigned_data_dir_wins_over_resolution(self, tmp_path):
        device_sentinel._DATA_DIR = tmp_path / "pinned"
        try:
            assert device_sentinel._data_dir() == tmp_path / "pinned"
            assert device_sentinel._baseline_file() == (
                tmp_path / "pinned" / "sentinel_baseline.json")
            assert device_sentinel._alerts_file() == (
                tmp_path / "pinned" / "sentinel_alerts.jsonl")
        finally:
            device_sentinel._DATA_DIR = None

    def test_assigned_file_paths_win_individually(self, tmp_path):
        device_sentinel._ALERTS_FILE = tmp_path / "a.jsonl"
        try:
            assert device_sentinel._alerts_file() == tmp_path / "a.jsonl"
            # The baseline is untouched, so it still resolves normally.
            assert device_sentinel._baseline_file().name == (
                "sentinel_baseline.json")
        finally:
            device_sentinel._ALERTS_FILE = None

    def test_the_hooks_exist_and_default_to_none(self):
        """monkeypatch.setattr needs the attribute to already exist, and two
        suites patch these by name. They default to None, not to a resolved
        path — see the next case for why that distinction is the whole point."""
        for name in ("_DATA_DIR", "_BASELINE_FILE", "_ALERTS_FILE"):
            assert getattr(device_sentinel, name) is None

    def test_a_monkeypatch_undo_does_not_pin_the_path(self, monkeypatch,
                                                      tmp_path):
        """The house override-hook standard, as a regression test.

        Served through a PEP 562 ``__getattr__`` instead, these names would
        report a RESOLVED path as their pre-patch value, and monkeypatch's undo
        would write that concrete path into the module globals — pinning it, so
        every later resolution ignores $PUFFPAI_DATA_DIR for the rest of the
        process. test_agent_events patches _ALERTS_FILE exactly this way.
        """
        with monkeypatch.context() as patched:
            patched.setattr(device_sentinel, "_ALERTS_FILE",
                            tmp_path / "pinned.jsonl")
            assert device_sentinel._alerts_file() == tmp_path / "pinned.jsonl"

        assert device_sentinel._ALERTS_FILE is None, (
            "the undo pinned a resolved path; resolution is now dead")
        # Still follows the environment afterwards.
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "after"))
        monkeypatch.setattr(device_sentinel, "_data_dir_cache", None)
        assert device_sentinel._alerts_file() == (
            tmp_path / "after" / "sentinel_alerts.jsonl")

    def test_an_unknown_attribute_still_raises(self):
        with pytest.raises(AttributeError):
            device_sentinel._NOT_A_REAL_PATH
