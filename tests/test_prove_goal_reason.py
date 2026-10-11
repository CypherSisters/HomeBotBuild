"""``goal_reaches_model`` says what the bot was told, not what it is called.

A real HomeBot run rendered ``Stargazer is told: Stargazer`` -- a PASS whose
evidence was the name twice over, because the record carried the name where the
goal belongs. Two things are pinned here:

  * the PASS line puts the GOAL after "is told", with the name as the subject
    of the sentence and nowhere else;
  * a "goal" that is only the bot's own name is a FAIL, not a PASS. A name is
    not a purpose, and a check that grades it as one is the failure mode this
    module exists to catch ("absence rendered as success").

Pure function, no install, no network.
"""
from __future__ import annotations

import pytest


def _reason(goal, name):
    from puffpai import prove
    return prove._check_goal(goal, name)


class TestTheGoalIsWhatItPrints:
    def test_the_goal_follows_is_told(self):
        from puffpai import prove
        check = _reason("Run our household calendar and lights", "Stargazer")
        assert check["result"] == prove.PASS
        assert "Run our household calendar and lights" in check["reason"]
        assert check["reason"].startswith("Stargazer is told")
        # The name appears once: as the subject, never as the purpose.
        assert check["reason"].count("Stargazer") == 1

    def test_a_long_goal_is_cut_not_dropped(self):
        from puffpai import prove
        goal = "keep the greenhouse " + "x" * 300
        check = _reason(goal, "Stargazer")
        assert check["result"] == prove.PASS
        assert "keep the greenhouse" in check["reason"]
        assert len(check["reason"]) < 200


class TestANameIsNotAPurpose:
    @pytest.mark.parametrize("goal", ["Stargazer", "stargazer", "STARGAZER",
                                      "  Stargazer  ", "Stargazer."])
    def test_the_name_in_the_goal_slot_is_a_fail(self, goal):
        from puffpai import prove
        check = _reason(goal, "Stargazer")
        assert check["result"] == prove.FAIL
        assert "its own name" in check["reason"]
        # Never the old sentence: no "<name> is told: <name>".
        assert "is told: Stargazer" not in check["reason"]

    def test_a_goal_that_merely_contains_the_name_still_passes(self):
        from puffpai import prove
        check = _reason("Stargazer runs the greenhouse", "Stargazer")
        assert check["result"] == prove.PASS
        assert "runs the greenhouse" in check["reason"]

    def test_no_goal_is_still_the_older_refusal(self):
        from puffpai import prove
        assert _reason("", "Stargazer")["result"] == prove.FAIL
        assert _reason("a goal", "")["result"] == prove.FAIL


class TestItIsTheSameCheckStaticProveRuns:
    def test_static_prove_grades_a_name_shaped_goal_as_a_gap(self):
        from puffpai import prove
        record = prove.static_prove(
            manifest={"persona": {"app": "stargazer", "name": "Stargazer",
                                  "goal": "Stargazer"}},
            sources={}, launchers={})
        check = next(c for c in record["checks"]
                     if c["id"] == "goal_reaches_model")
        assert check["result"] == prove.FAIL
        assert any(gap.get("skill") == "prove" for gap in record["gaps"])
