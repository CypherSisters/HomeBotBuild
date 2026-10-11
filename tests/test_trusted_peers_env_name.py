"""No file names an environment variable this build does not read.

``start_api.py`` told an operator to list Home Assistant's IP in
``$PUFFPAI_TRUSTED_PEERS``. The variable ``security/endpoint_gate.py`` actually
reads is ``PUFFPAI_TRUSTED_LOCAL_PEERS``. Following the comment sets a variable
nothing looks at, the peer is not trusted, and the operator's next move -- on a
box whose API key HA's integration cannot attach -- is to widen the bind address
or hand out the key. A wrong variable name in a security instruction is a
security bug.

The check is by prefix rather than by a list of files, so a new doc cannot
reintroduce the short name somewhere this file does not look.
"""
from __future__ import annotations

import re
from pathlib import Path

from puffpai.security import endpoint_gate

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Every PUFFPAI_TRUSTED_* spelling, wherever it appears.
TRUSTED_VAR_RE = re.compile(r"PUFFPAI_TRUSTED[A-Z_]*")

SEARCH_ROOTS = (REPO_ROOT / "src", REPO_ROOT / "start_api.py")


def _files():
    for root in SEARCH_ROOTS:
        if root.is_file():
            yield root
        else:
            yield from sorted(root.rglob("*.py"))


def test_the_env_var_name_is_the_one_the_gate_reads():
    assert endpoint_gate.TRUSTED_PEERS_ENV == "PUFFPAI_TRUSTED_LOCAL_PEERS"


def test_no_source_file_names_a_different_trusted_peers_variable():
    offenders = []
    for path in _files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name in set(TRUSTED_VAR_RE.findall(text)):
            if name != endpoint_gate.TRUSTED_PEERS_ENV:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {name}")
    assert not offenders, (
        "these name a trusted-peers variable nothing reads: "
        + "; ".join(sorted(offenders)))


def test_start_api_points_at_the_real_variable():
    text = (REPO_ROOT / "start_api.py").read_text(encoding="utf-8")
    assert endpoint_gate.TRUSTED_PEERS_ENV in text
