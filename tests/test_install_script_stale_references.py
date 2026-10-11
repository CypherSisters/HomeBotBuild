"""No stale-org or stale-branding references in scripts/, and
scripts/install_ubuntu.sh --dry-run must not require root.

* ``kiwix`` names (the retired server, the old ``kiwix_rag_db`` ChromaDB
  folder), stale org placeholders (the misspelled org name,
  ``yourusername``) and the retired ``dolphin-llama3`` model name must not
  appear anywhere in scripts/;
* ``install_ubuntu.sh --dry-run`` must run without root, so the documented
  "print every command, run none" mode works for a stranger evaluating the
  installer without sudo.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO / "scripts"

#: Case-insensitive; any hit anywhere under scripts/ is a regression. The
#: stale Kiwix pieces are the retired server and the old ChromaDB folder
#: name. Naming Kiwix as the publisher of a ZIM pack (the Gutenberg intake
#: scripts do) is attribution, not stale branding, so the bare word is
#: allowed.
BANNED_PATTERN = re.compile(
    r"kiwix[-_ ]?serve|kiwix_rag_db|kiwix-tools|libkiwix|dolphin-llama3|"
    r"yourusername|c[i]phersisters", re.I)


def _script_files():
    return sorted(p for p in SCRIPTS_DIR.rglob("*") if p.is_file())


def test_scripts_are_free_of_stale_branding_and_org_references() -> None:
    hits = []
    for path in _script_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for m in BANNED_PATTERN.finditer(text):
            hits.append(f"{path.relative_to(REPO)}: {m.group(0)!r}")
    assert not hits, "stale reference(s) survive in scripts/:\n" + "\n".join(hits)


def test_install_ubuntu_dry_run_does_not_require_root() -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available on this system")
    installer = SCRIPTS_DIR / "install_ubuntu.sh"
    proc = subprocess.run(
        [bash, str(installer), "--dry-run"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = proc.stdout + proc.stderr
    assert "ERROR: run as root" not in combined, (
        f"--dry-run still refuses to run without root:\n{combined}"
    )
    # A real Linux box will run the whole dry-run and exit 0. This may also
    # run on a non-Linux CI shell (e.g. Git Bash on Windows), where the
    # installer's own Linux-only preflight (missing /etc/os-release) is an
    # acceptable, explicit stop — anything else is a regression.
    if proc.returncode != 0:
        assert "unsupported OS" in combined or "os-release" in combined, (
            f"--dry-run failed for an unexpected reason (exit {proc.returncode}):\n{combined}"
        )


def test_provision_orin_dry_run_does_not_require_root() -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available on this system")
    installer = SCRIPTS_DIR / "provision_orin.sh"
    proc = subprocess.run(
        [bash, str(installer), "--dry-run"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = proc.stdout + proc.stderr
    assert "This script must run as root" not in combined, (
        f"--dry-run still refuses to run without root:\n{combined}"
    )
    if proc.returncode != 0:
        assert "unsupported OS" in combined or "os-release" in combined, (
            f"--dry-run failed for an unexpected reason (exit {proc.returncode}):\n{combined}"
        )
