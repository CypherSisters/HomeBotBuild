"""CONTRIBUTING.md must tell a contributor to sync with `main`.

This repo's only branch is `main`: the sync block and the PR checklist must
name it, and no instruction may send anyone to a retired branch (the pattern
below). The guard is cheap and standing, so a doc merged down from upstream
cannot quietly reintroduce one.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

#: The retired upstream branch, written as a pattern so its name never appears
#: in this repo as a literal.
_RETIRED_BRANCH = r"orin[-]build"

#: A git command that names the retired branch to push, pull or track.
RETIRED_BRANCH_RE = re.compile(
    r"git\s+push\s+\S+\s+" + _RETIRED_BRANCH + r"\b"
    r"|git\s+pull\s+\S+\s+" + _RETIRED_BRANCH + r"\b"
    r"|git\s+checkout\s+" + _RETIRED_BRANCH + r"\b"
    r"|up\s+to\s+date\s+with\s+.?" + _RETIRED_BRANCH + r"\b",
    re.IGNORECASE)


def test_contributing_exists():
    assert CONTRIBUTING.exists()


def test_no_instruction_points_at_a_retired_branch():
    text = CONTRIBUTING.read_text(encoding="utf-8")
    hits = RETIRED_BRANCH_RE.findall(text)
    assert not hits, (
        "CONTRIBUTING.md points contributors at a retired branch, which does "
        "not exist in this repo: %r" % hits)


def test_the_sync_block_pushes_to_main():
    text = CONTRIBUTING.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "git checkout main" in text
    assert "git pull upstream main" in text
    assert "git push origin main" in text


def test_the_pr_checklist_names_main():
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "Branch is up to date with `main`" in text
