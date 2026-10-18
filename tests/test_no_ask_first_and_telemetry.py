"""Two promises kept honest by grep.

1. The "ask me before every outside visit" style is gone. Nothing ever enforced it; no shipped text may claim it, the
   wizard may not offer it, and an old record that still says "moat" is read
   as "fenced" with one plain note.
2. Zero telemetry: the web page's build tool (Next.js) reports usage to its
   maker unless NEXT_TELEMETRY_DISABLED=1 is set. Both installers, every
   launcher they write, and the dashboard's env template set it. And the
   installers make a secret file owner-only BEFORE the secret goes in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from puffpai import zone_bridge

ROOT = Path(__file__).resolve().parents[1]

#: Claims that the bot asks before each outside visit. Negations ("was never
#: built") are fine; a claim or an offer is not.
_ASK_FIRST_CLAIMS = re.compile(
    r"asks? (?:you|me) first"
    r"|ask me (?:before each|before every|every time)"
    r"|asks? (?:you |me )?before (?:each|every) outside visit"
    r"|every outbound call asks"
    r"|stricter \(asking",
    re.I)

_SHIPPED = [ROOT / "src", ROOT / "config", ROOT / "docs",
            ROOT / "dashboard" / "app"]
_EXTS = {".py", ".json", ".md", ".ts", ".tsx", ".txt"}


def _shipped_files():
    for base in _SHIPPED:
        for path in base.rglob("*"):
            if path.suffix in _EXTS and "node_modules" not in path.parts \
                    and "catalog_snapshots" not in path.parts:
                yield path
    for name in ("README.md", "SECURITY.md"):
        if (ROOT / name).exists():
            yield ROOT / name


def test_no_shipped_text_claims_it_asks_before_every_outside_visit():
    hits = []
    for path in _shipped_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in _ASK_FIRST_CLAIMS.finditer(text):
            hits.append(f"{path.relative_to(ROOT)}: {m.group(0)!r}")
    assert not hits, "\n".join(hits)


def test_the_ban_itself_bites():
    for claim in ("Your bot asks you first.", "Ask me before each outside visit",
                  "Stricter (asking every time is not built yet)",
                  "every outbound call asks you"):
        assert _ASK_FIRST_CLAIMS.search(claim), claim


# ── zero telemetry ───────────────────────────────────────────────────────────

def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")

