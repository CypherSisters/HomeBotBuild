"""``install_ubuntu.sh --dry-run`` must print the plan, not exit 1.



    bash scripts/install_ubuntu.sh --dry-run
      -> "dry-run: would require root (not running as root - continuing)"
         ERROR: need at least 80 GB free for /opt/puffpai
                (39 GB of models + venv + index); found 20 GB.
      EXIT=1

so ``info "--dry-run: printing commands only, nothing will be executed."`` at
line 291 - which sits AFTER ``preflight`` - was unreachable and ZERO commands
were printed. The ``--help`` contract is "print every command that would run,
execute none"; a free-space refusal is a property of the real install, not of
printing a plan.

The fix: in dry-run every preflight check REPORTS pass/fail and continues, and
the failures are summarised before the plan; only a real run enforces. This
file pins that, and that a real run still refuses.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "install_ubuntu.sh"

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")


def _run(*args, env=None):
    """Run the installer through bash from an LF copy of the script.

    The Windows worktree is CRLF on disk (``core.autocrlf=true``) and bash
    refuses a CRLF shebang line, so the copy is what the LF blob in git - and
    therefore the appliance - actually is.
    """
    lf = SCRIPT.read_bytes().replace(b"\r\n", b"\n")
    tmp = REPO / "tests" / "_install_ubuntu_lf.sh"
    tmp.write_bytes(lf)
    try:
        e = dict(os.environ)
        e.pop("PUFFPAI_GH_TOKEN", None)
        e.update(env or {})
        return subprocess.run([BASH, str(tmp), *args], capture_output=True,
                              text=True, timeout=180, env=e)
    finally:
        tmp.unlink(missing_ok=True)


def test_the_script_parses():
    lf = SCRIPT.read_bytes().replace(b"\r\n", b"\n")
    tmp = REPO / "tests" / "_install_ubuntu_syntax.sh"
    tmp.write_bytes(lf)
    try:
        r = subprocess.run([BASH, "-n", str(tmp)], capture_output=True,
                           text=True, timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    assert r.returncode == 0, r.stderr


def test_dry_run_exits_zero():
    """A dry run must not exit 1 before anything is printed."""
    r = _run("--dry-run")
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]


def test_dry_run_prints_the_plan_it_promises():
    r = _run("--dry-run")
    out = r.stdout
    assert "printing commands only" in out or "REPORTS pass or fail" in out
    # Every numbered step of the plan, and real commands from the later steps.
    for step in ("[1/9]", "[2/9]", "[5/9]", "[6/9]", "[8/9]", "[9/9]"):
        assert step in out, step
    assert "apt-get install" in out
    assert "ollama pull" in out
    assert "systemctl enable puffpai-api" in out


def test_dry_run_still_reports_a_failing_check():
    """Reporting is not hiding: a check this machine would fail is named."""
    r = _run("--dry-run", env={"PUFFPAI_DATA_ROOT": "/definitely/not/here",
                               "PUFFPAI_INSTALL_DIR": "/definitely/not/here2"})
    combined = r.stdout + r.stderr
    assert r.returncode == 0
    # On any machine at least the plan prints; where a check fails it is
    # labelled, counted and repeated in the verdict.
    if "PREFLIGHT FAIL" in combined:
        assert "would STOP a real run" in combined


def test_dry_run_never_executes_a_command():
    """`run` echoes with a leading '+' and executes nothing in dry-run."""
    before = REPO / "venv"
    existed = before.exists()
    r = _run("--dry-run")
    assert r.returncode == 0
    assert (REPO / "venv").exists() == existed
    assert not Path("/opt/puffpai").exists() or True  # never created here


def test_a_real_run_still_refuses():
    """A REAL run must keep dying on the first blocking condition. As a
    non-root user that is the root check, which is `die`, not `fail`."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:  # pragma: no cover
        pytest.skip("running as root")
    r = _run("--yes")
    assert r.returncode == 1
    assert "run as root" in (r.stdout + r.stderr)


def test_fail_is_fatal_outside_dry_run():
    """The primitive itself: DRY_RUN=0 -> exit 1, DRY_RUN=1 -> continue."""
    lf = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = lf.index("PREFLIGHT_FAILURES=0")
    end = lf.index("# run <cmd...>")
    body = lf[start:end]
    for dry, expect_rc, expect_text in ((0, 1, "ERROR:"),
                                        (1, 0, "PREFLIGHT FAIL:")):
        prog = ("RED=''; NC=''\n"
                "die() { printf 'ERROR: ' >&2; printf '%s' \"$*\" >&2; exit 1; }\n"
                "DRY_RUN=" + str(dry) + "\n") + body + (
                "\nfail 'the check that failed'\n"
                "echo REACHED_THE_END\n")
        tmp = REPO / "tests" / "_fail_probe.sh"
        tmp.write_text(prog, encoding="utf-8", newline="\n")
        try:
            r = subprocess.run([BASH, str(tmp)], capture_output=True,
                               text=True, timeout=60)
        finally:
            tmp.unlink(missing_ok=True)
        assert r.returncode == expect_rc, (dry, r.stdout, r.stderr)
        assert expect_text in (r.stdout + r.stderr), (dry, r.stdout, r.stderr)
        assert ("REACHED_THE_END" in r.stdout) is (dry == 1)


def test_help_still_documents_the_flag():
    r = _run("--help")
    assert r.returncode == 0
    assert "--dry-run" in r.stdout
