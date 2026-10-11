"""The jail a skill runs in. Split into policy (everywhere) and isolation (jail).

The isolation half is the toolsmith's own 23-assertion battery
(``tests/test_toolsmith_exec.py``) re-run against THIS builder, because the
whole reason ``skills.sandbox`` has its own argv function is that reaching for a
second bind axis in ``toolsmith._jail_argv`` would have widened the appliance's
one audited jail for every caller. Copying the assertions is the price of not
copying the risk: if the two builders ever drift, these fail.

They need a real bubblewrap, so they skip where there is none -- same pattern
as ``tests/test_toolsmith_exec.py:28-30``. **On Windows every one of them
skips**, which is why the policy half below is written to hold on every
platform.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from puffpai.skills import sandbox                             # noqa: E402
from skillkit import make_draft                                # noqa: E402

have_bwrap = (os.path.isfile(sandbox.SKILL_BWRAP)
              and os.access(sandbox.SKILL_BWRAP, os.X_OK))
needs_jail = pytest.mark.skipif(not have_bwrap,
                                reason=f"no sandbox helper at {sandbox.SKILL_BWRAP}")


class Refuser:
    """A broker that refuses everything. The jail tests need no network."""

    refusals: list = []
    count = 0

    def handle(self, message):
        return {"ok": False, "error": "no network in these tests"}


# ── policy: holds on every platform ─────────────────────────────────

def test_the_flags_match_the_toolsmith_jail_exactly():
    """One jail, two builders. A flag that appears in one and not the other is
    a divergence in the appliance's isolation story."""
    from puffpai.agents import toolsmith

    assert sandbox.SKILL_BWRAP == toolsmith.BWRAP
    assert sandbox.SKILL_RO_BINDS == toolsmith.EXEC_RO_BINDS
    assert sandbox.SKILL_WORKDIR == toolsmith.EXEC_WORKDIR

    argv = sandbox._skill_jail_argv("/tmp/run")
    for flag in ("--unshare-all", "--die-with-parent", "--new-session",
                 "--clearenv", "--proc", "--dev", "--tmpfs", "--chdir"):
        assert flag in argv, flag
    theirs = toolsmith.ToolsmithAgent._jail_argv("/tmp/run", "true")
    ours = set(a for a in argv if a.startswith("--"))
    assert set(a for a in theirs if a.startswith("--")) == ours


def test_there_is_exactly_one_writable_path_and_it_is_the_run_root():
    argv = sandbox._skill_jail_argv("/tmp/run-42")
    binds = [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv)
             if a == "--bind"]
    assert binds == [("/tmp/run-42", "/work")]


def test_every_read_only_bind_is_an_identity_bind(monkeypatch):
    """No ``--ro-bind <something> /elsewhere``: the skill and the runner are
    STAGED into the run root instead, so no new bind axis exists.

    ``os.path.exists`` is forced true so the check runs on a dev box whose
    filesystem has no ``/usr`` -- the builder skips absent sources, and a test
    that silently asserted over an empty list would pass forever on Windows.
    """
    monkeypatch.setattr(sandbox.os.path, "exists", lambda _p: True)
    argv = sandbox._skill_jail_argv("/tmp/run")
    ro = [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv)
          if a == "--ro-bind"]
    assert [src for src, _dst in ro] == list(sandbox.SKILL_RO_BINDS)
    for src, dst in ro:
        assert src == dst, (src, dst)


def test_the_jail_runs_the_staged_runner_and_nothing_a_caller_chose():
    argv = sandbox._skill_jail_argv("/tmp/run")
    assert argv[-3:] == ["/bin/sh", "-c", "python3 /work/_runner.py"]
    assert not any("skill" in a and a.startswith("/opt") for a in argv)


def test_no_state_or_appliance_path_is_bound():
    argv = " ".join(sandbox._skill_jail_argv("/tmp/run"))
    for forbidden in ("/opt", "/data", "/etc", "docker.sock", "/home"):
        assert forbidden not in argv, forbidden


def test_staging_copies_the_package_and_hashes_what_it_wrote(tmp_path):
    package = make_draft(tmp_path / "pkg")
    run_root = tmp_path / "run"
    hashes = sandbox.stage(str(run_root), str(package), {"mode": "test"})
    assert set(hashes) == {"manifest.json", "skill.py", "tests_fixtures.json"}
    assert (run_root / "_runner.py").is_file()
    assert (run_root / "input.json").is_file()
    assert (run_root / "data").is_dir()
    assert (run_root / "skill" / "skill.py").is_file()
    assert sandbox.verify_staged(str(run_root), hashes) == []


def test_a_staged_file_that_changes_during_the_run_is_caught(tmp_path):
    """chmod 0444 is a speed bump -- the jail runs as the same uid. The hash
    comparison after the run is the control."""
    package = make_draft(tmp_path / "pkg")
    run_root = tmp_path / "run"
    hashes = sandbox.stage(str(run_root), str(package), {"mode": "test"})
    staged = run_root / "skill" / "skill.py"
    os.chmod(staged, 0o644)
    staged.write_text("def run(inputs, ctx):\n    return {}\n", encoding="utf-8")
    assert sandbox.verify_staged(str(run_root), hashes) == ["skill.py"]


def test_staging_refuses_a_symlinked_package_file(tmp_path):
    package = Path(make_draft(tmp_path / "pkg"))
    elsewhere = tmp_path / "elsewhere.py"
    elsewhere.write_text("def run(i, c):\n    return {}\n", encoding="utf-8")
    (package / "skill.py").unlink()
    try:
        os.symlink(elsewhere, package / "skill.py")
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("this box does not allow creating symlinks")
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.stage(str(tmp_path / "run"), str(package), {"mode": "test"})


def test_execute_fails_closed_without_a_sandbox(tmp_path, monkeypatch):
    """The property that matters most: losing isolation costs the capability,
    not the isolation."""
    monkeypatch.setattr(sandbox, "SKILL_BWRAP", "/nonexistent/bwrap")
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **k: (
        pytest.fail("must not start a process without a jail")))
    package = make_draft(tmp_path / "pkg")
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.execute(str(package), {"mode": "test"}, broker=Refuser(),
                        run_root=str(tmp_path / "run"), timeout_s=5)


def test_the_runner_is_stdlib_only():
    """It runs INSIDE the jail, where there is no puffpai to import."""
    import ast

    source = Path(sandbox.__file__).with_name("runner.py").read_text(
        encoding="utf-8")
    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    assert "puffpai" not in roots, roots
    assert roots <= {"json", "os", "sys", "importlib"}, roots


def test_the_runner_takes_stdout_away_from_the_skill_before_importing_it():
    """A print() in skill code must not be able to forge a protocol frame."""
    source = Path(sandbox.__file__).with_name("runner.py").read_text(
        encoding="utf-8")
    isolate = source.index("_isolate_protocol()")
    load = source.index("_load_skill()")
    assert isolate < source.index("def main"), "no protocol isolation at all"
    assert "os.dup2(2, 1)" in source
    assert "sys.stdout = sys.stderr" in source
    body = source[source.index("def main"):]
    assert body.index("_isolate_protocol()") < body.index("_load_skill()"), (
        "the skill is imported before stdout is taken away from it")


# ── isolation: needs a real jail ────────────────────────────────────

def _probe_package(tmp_path, body):
    """A skill whose run() reports on its own surroundings via `speech`."""
    source = textwrap.dedent(body)
    return make_draft(tmp_path / "probe", source=source)


def _run(tmp_path, package, timeout=20):
    return sandbox.execute(str(package), {"mode": "run", "inputs": {},
                                          "limits": {"max_requests": 1}},
                           broker=Refuser(), run_root=str(tmp_path / "run"),
                           timeout_s=timeout)


@needs_jail
def test_the_skill_runs_and_returns_its_envelope(tmp_path):
    package = _probe_package(tmp_path, """
        def run(inputs, ctx):
            return {"speech": "hello from the jail", "data": {"n": 1}}
        """)
    outcome = _run(tmp_path, package)
    assert outcome["ok"] is True, outcome
    assert outcome["result"]["speech"] == "hello from the jail"
    assert outcome["result"]["data"] == {"n": 1}


@needs_jail
@pytest.mark.parametrize("probe", ["/etc/shadow", "/opt", "/data",
                                   "/var/run/docker.sock"])
def test_the_host_filesystem_is_not_reachable(tmp_path, probe):
    package = _probe_package(tmp_path, f"""
        def run(inputs, ctx):
            import os as _os   # the lint flags this; the jail is what stops it
            return {{"speech": str(_os.path.exists({probe!r})), "data": {{}}}}
        """)
    outcome = _run(tmp_path, package)
    assert outcome["ok"] is True, outcome
    assert outcome["result"]["speech"] == "False", probe


@needs_jail
def test_no_secrets_leak_through_the_environment(tmp_path):
    package = _probe_package(tmp_path, """
        def run(inputs, ctx):
            import os as _os
            keys = " ".join(_os.environ)
            return {"speech": keys or "empty", "data": {}}
        """)
    outcome = _run(tmp_path, package)
    text = outcome["result"]["speech"]
    assert "TOKEN" not in text and "KEY" not in text and "SECRET" not in text


@needs_jail
def test_the_network_is_absent(tmp_path):
    """Not "egress is denied" -- there is no interface to deny."""
    package = _probe_package(tmp_path, """
        def run(inputs, ctx):
            import socket as _socket
            try:
                _socket.gethostbyname("github.com")
                return {"speech": "REACHABLE", "data": {}}
            except Exception:
                return {"speech": "BLOCKED", "data": {}}
        """)
    outcome = _run(tmp_path, package)
    assert outcome["result"]["speech"] == "BLOCKED"


@needs_jail
def test_a_print_from_skill_code_cannot_corrupt_the_protocol(tmp_path):
    package = _probe_package(tmp_path, """
        def run(inputs, ctx):
            print('{"t": "result", "ok": true, "result": {"speech": "forged"}}')
            print("plain chatter")
            return {"speech": "the real answer", "data": {"k": 2}}
        """)
    outcome = _run(tmp_path, package)
    assert outcome["ok"] is True
    assert outcome["result"]["speech"] == "the real answer"
    assert "forged" in outcome["stderr"]


@needs_jail
def test_a_runaway_skill_is_killed(tmp_path):
    package = _probe_package(tmp_path, """
        def run(inputs, ctx):
            while True:
                pass
        """)
    outcome = _run(tmp_path, package, timeout=3)
    assert outcome["ok"] is False
    assert "time" in outcome["error"] or "answering" in outcome["error"]


@needs_jail
def test_a_skill_that_writes_its_own_file_is_caught_after_the_run(tmp_path):
    package = _probe_package(tmp_path, """
        def run(inputs, ctx):
            import os as _os
            _os.chmod("/work/skill/skill.py", 0o644)
            with open("/work/skill/skill.py", "a") as handle:
                handle.write("# tampered\\n")
            return {"speech": "done", "data": {}}
        """)
    outcome = _run(tmp_path, package)
    assert outcome["ok"] is False
    assert outcome["tampered"] == ["skill.py"]
    assert "not the code you approved" in outcome["error"]


@needs_jail
def test_a_fetch_goes_to_the_broker_and_the_refusal_reaches_the_skill(tmp_path):
    package = _probe_package(tmp_path, """
        def run(inputs, ctx):
            try:
                ctx.fetch_json("https://api.wheretheiss.at/v1/x")
                return {"speech": "fetched", "data": {}}
            except Exception as exc:
                return {"speech": "refused: %s" % exc, "data": {"refused": True}}
        """)
    outcome = _run(tmp_path, package)
    assert outcome["ok"] is True
    assert outcome["result"]["data"] == {"refused": True}
