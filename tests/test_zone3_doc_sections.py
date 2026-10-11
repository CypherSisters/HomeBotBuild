"""Every section the Zone 3 step cites must exist in the document it cites.

``zone_bridge.ZONE3_DOC_SECTIONS`` is printed to the owner, written into
``manifest.bridge.edge.zone3.doc_sections`` and recorded in the pending-apply
file, as "read these parts of docs/ZONE3_SETUP.md". It is a citation, not prose,
and it drifted: the doc renamed Step 1 from "Start Pi-hole" to "Start AdGuard
Home" (this build ships AdGuard, not Pi-hole) and the constant kept pointing at
a heading that no longer existed. An owner following it looks for a section that
is not there, on the one step where they are being asked to run commands
themselves.

Cheap and standing, so the next rename fails here instead of in someone's house.
"""
from __future__ import annotations

import re
from pathlib import Path

from puffpai import zone_bridge

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A markdown ATX heading: the hashes, then the text we compare verbatim.
HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.MULTILINE)


def _headings() -> set[str]:
    doc = REPO_ROOT / zone_bridge.ZONE3_DOC
    text = doc.read_text(encoding="utf-8").replace("\r\n", "\n")
    return set(HEADING_RE.findall(text))


def test_the_cited_document_exists():
    assert (REPO_ROOT / zone_bridge.ZONE3_DOC).is_file()


def test_every_cited_section_heading_exists_verbatim():
    """``A - B`` cites subsection B of section A; both halves must be real."""
    headings = _headings()
    missing = []
    for citation in zone_bridge.ZONE3_DOC_SECTIONS:
        for part in [p.strip() for p in citation.split(" - ")]:
            if part and part not in headings:
                missing.append(f"{citation!r} -> {part!r}")
    assert not missing, (
        "ZONE3_DOC_SECTIONS cites headings that are not in "
        f"{zone_bridge.ZONE3_DOC}: " + "; ".join(missing))


def test_step_one_names_the_engine_this_build_ships():
    """AdGuard Home, not Pi-hole: the container the commands actually start."""
    assert any("AdGuard" in s for s in zone_bridge.ZONE3_DOC_SECTIONS)
    assert not any("Pi-hole" in s for s in zone_bridge.ZONE3_DOC_SECTIONS)
