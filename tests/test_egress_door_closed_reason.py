"""04: a door-closed reason must never be swallowed by the planner.

a door-gated read reached the
planner route, which returned a generic "Sorry, I could not complete that one.
Could you rephrase?" while the tool was never invoked. The tool own refusal was
always correct — the planner discarded it, so the owner could not tell "the
appliance refused to phone home" from "the appliance broke". Door-gated reads
are dispatched by ``_CAPREAD_RULES`` before the planner, so the door own
``user_message`` is what reaches the user.

The security contract is untouched: nothing in that dispatch table opens a
door, widens a host list or bypasses ``require_open`` — and this module is
what keeps that true. A capread rule is a *routing* decision; the moment one of
them could consult or flip an egress flag, routing would become consent.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402


def test_the_capread_table_cannot_open_a_door():
    """Guard against a future edit turning a refusal into a bypass."""
    src = Path(api.__file__).with_suffix(".py").read_text(
        encoding="utf-8", errors="replace")
    block = src[src.index("_CAPREAD_RULES = ["):]
    block = block[:block.index("\ndef _try_capability_read")]
    for forbidden in ("egress_allowed", "require_open", "PUFFPAI_EGRESS",
                      "egress_get"):
        assert forbidden not in block, (
            forbidden + " appears in the capread dispatch table; routing must "
            "not be able to grant or check egress consent")


def test_every_capread_rule_names_a_tool_and_a_pattern():
    """A rule with no tool, or no trigger, is a silent dead branch."""
    assert api._CAPREAD_RULES
    for row in api._CAPREAD_RULES:
        name, include = row[0], row[1]
        assert isinstance(name, str) and name, row
        assert include is not None, row
    assert set(api._CAPREAD_ZERO_ARG) <= {r[0] for r in api._CAPREAD_RULES}
