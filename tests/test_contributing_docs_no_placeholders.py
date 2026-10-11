"""Public docs carry no leaked email or placeholder.

A cheap, standing guard over CONTRIBUTING.md, SECURITY.md and docs/: no ``@``
email pattern (a made-up security contact address), and no leftover
placeholder token (``YOUR_USERNAME``, ``yourusername``, the misspelled org name),
such as a ``YOUR_USERNAME`` in a clone command.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PLACEHOLDER_RE = re.compile(r"YOUR_USERNAME|yourusername|c[i]phersisters",
                             re.IGNORECASE)


def _root_docs() -> list[Path]:
    files = []
    for name in ("CONTRIBUTING.md", "SECURITY.md"):
        p = REPO_ROOT / name
        if p.exists():
            files.append(p)
    return files


def _target_files() -> list[Path]:
    files = list(_root_docs())
    docs_dir = REPO_ROOT / "docs"
    if docs_dir.exists():
        files.extend(sorted(docs_dir.rglob("*.md")))
    return files


@pytest.mark.parametrize("path", _root_docs(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_email_addresses(path: Path) -> None:
    # Scoped to CONTRIBUTING.md/SECURITY.md (the files that actually leaked
    # a report-by-email address). docs/ is excluded here because several
    # ops docs legitimately contain user@host systemd-unit / service
    # identifiers (e.g. "cryptsetup@puffpai-data.service") that match an
    # email-shaped regex but are not email addresses.
    text = path.read_text(encoding="utf-8")
    hits = EMAIL_RE.findall(text)
    assert not hits, f"{path} contains email address(es): {hits}"


@pytest.mark.parametrize("path", _target_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_placeholder_tokens(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    hits = PLACEHOLDER_RE.findall(text)
    assert not hits, f"{path} contains placeholder token(s): {hits}"


def test_target_files_found() -> None:
    """Guard against the parametrize silently collecting zero files."""
    assert (REPO_ROOT / "CONTRIBUTING.md").exists()
    assert (REPO_ROOT / "SECURITY.md").exists()
