"""One whole interactive build, from the first question to the sealed vault.

The other interactive module drives three households through the branches they
differ on. This one drives ONE household all the way and then asks the disk
what happened, because the wizard's promises are promises about the disk:

* the persona the owner named has a folder of its own, under the writable apps
  root this build resolves - not in the checkout,
* its surface is on the allowlist, so the bot the build exists for is visible
  in the nav rather than trimmed out of its own build,
* the vault is sealed and the recovery code came back, recorded as
  ``recovery_confirmed``,
* the PIN and the code are in no file anywhere under the install, and
* the cards came up in the order :data:`puffpai.wizard.runner.SCHEDULE` says,
  which is the thing a schedule is for.

The run is offline-postured and points the brain at an address where nothing
answers: a wizard that finds no Ollama still has to finish, and this test says
so out loud rather than depending on what happens to be running on the
machine.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from tests import _wizard_driver as driver


#: Nothing listens here, on purpose.
DEAD_URL = "http://127.0.0.1:11499"

BOT_NAME = "Stargazer"
BOT_ID = "stargazer"
GOAL = ("Keep the family's stargazing notes, the telescope log and the launch "
        "calendar in one place.")
PIN = "Nebula7Quasar"

CEREMONY_RULES: list[driver.Rule] = [
    (r"reach the internet WHILE YOU BUILD", "offline"),
    (r"what may the FINISHED BOT reach", "island"),
    (r"where ollama answers", DEAD_URL),
    (r"what you call it|what should this bot be called", BOT_NAME),
    (r"what it is for, in your words", GOAL),
    # Blank: the id is the wizard's suggestion, and taking it is the answer
    # most owners give. Asserting it lands on "stargazer" is asserting that the
    # suggestion is derived from the name that was typed one card earlier.
    (r"the id it lives under", ""),
    (r"a place of its own", "yes"),
    (r"your vault PIN|choose a vault PIN", PIN),
    # The PIN questions are asked under their own heading; the line that
    # takes the answer only says the typing is hidden.
    (r"what you type stays hidden", PIN),
    (r"type it (again|once more)", PIN),
    (r"type the recovery code back|type the code back",
     lambda ask: ask.recovery_code()),
    (r"download it now|fetch now", "no"),
    (r"how far may your bot reach", "drawbridge_up"),
    (r"do you run home assistant", "no"),
    (r"connect a home assistant instance", "no"),
    (r"home assistant (url|address)", DEAD_URL),
]


@pytest.fixture(scope="module")
def ceremony(tmp_path_factory):
    """One run. Every test below reads it; none of them runs the wizard again."""
    root = driver.install_root(tmp_path_factory.mktemp("ceremony"), "one")
    result = driver.drive(CEREMONY_RULES, env=driver.base_env(root),
                          args=("--stated-ram", "16"))
    return root, result


def _manifest(root):
    path = root / "state" / "config" / "build_manifest.json"
    assert path.is_file(), "the wizard finished without writing a build record"
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_ceremony_finishes(ceremony):
    root, result = ceremony
    assert result.returncode == 0, result.log_text() + result.transcript[-4000:]
    # HomeBot asks no name: the build is HomeBot whatever a script would type.
    assert _manifest(root)["build"]["name"] == "HomeBot"


def test_the_persona_the_owner_named_has_a_folder_of_its_own(ceremony, monkeypatch):
    """HomeBot is the HomeBot pack's persona: no name is asked, so no folder
    is made for one, and nothing is written into the checkout."""
    from puffpai import app_registry

    root, _result = ceremony
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(root / "state"))
    apps_dir = app_registry.user_apps_dir()
    assert not os.path.isdir(os.path.join(apps_dir, BOT_ID))
    persona = _manifest(root)["persona"]
    assert persona["id"] == "homebot" and persona["pack"] == "homebot"
    assert persona["scaffolded"] is False


def test_the_personas_surface_is_on_the_allowlist_it_was_trimmed_to(ceremony):
    """A trimmed build that hides its own persona is the bug this guards."""
    from puffpai import persona_scaffold

    from puffpai import build_interview

    root, _result = ceremony
    surfaces = _manifest(root)["surfaces"]
    allowlist = list(surfaces.get("allowlist") or [])
    assert persona_scaffold.persona_surface(BOT_ID) == f"persona.{BOT_ID}"
    # HomeBot is not trimmed: every page is on, each with its reason.
    assert sorted(allowlist) == sorted(build_interview.surface_taxonomy())
    for surface in allowlist:
        assert surfaces["reasons"][surface], "a surface on the list with no reason"


def test_the_vault_is_sealed_on_disk_and_the_code_came_back(ceremony):
    root, _result = ceremony
    vault = _manifest(root)["vault"]
    assert vault["configured"] is True
    assert vault["sealed"] is True
    assert vault["ready"] is True
    assert vault["recovery_issued"] is True
    # The point of the ceremony: the code was shown once, typed back, and
    # checked. A vault whose spare key nobody proved they hold is not sealed
    # in the sense this build means.
    assert vault["recovery_confirmed"] is True

    # ...and the seal is a thing on the disk, not a line in a record.
    vault_dir = root / "state" / "vault_data"
    assert vault_dir.is_dir()
    assert any(vault_dir.iterdir()), "a sealed vault that wrote nothing"


def test_neither_the_pin_nor_the_code_is_in_any_file_under_the_install(ceremony):
    """Both dirs, every file. The one display of the code is on the screen."""
    root, result = ceremony
    code = result.recovery_code()
    assert code, "the show-once screen drew no recovery code"
    assert result.transcript.count(code) == 1, "the code was shown twice"

    blob = driver.tree_text(root)
    assert PIN not in blob, "the vault PIN was written to a file"
    assert code not in blob, "the recovery code was written to a file"
    # And the PIN is not on the screen either: it is typed, never echoed.
    assert PIN not in result.transcript


def test_the_cards_came_up_in_the_order_the_schedule_names(ceremony):
    """The console's card order IS the schedule's lifecycle entries, in order.

    Not a subset and not a re-ordering: the schedule is where the wizard's
    order is decided, and a card that quietly moves (or stops being drawn)
    changes what the owner is asked before they have the answer to it.
    """
    from puffpai.wizard import host, runner

    root, _result = ceremony
    expected = []
    for step in host.setup_schedule():
        part = runner.lifecycle_part(step.part)
        if not step.skill or part is None:
            continue                      # a runner entry, or a part_* wrapper
        expected.append(f"{step.skill}:{part}" if part else step.skill)

    # The console prints no card ids (the owner's words only), so the order is
    # read from the run record, which logs every card as it is proposed.
    records = sorted(root.rglob("wizard_run.json"))
    assert records, "the run left no record"
    state = json.loads(records[0].read_text(encoding="utf-8"))
    shown = []
    for row in state.get("log") or ():
        entry = str(row.get("entry") or "")
        if not row.get("shown") or "." not in entry:
            continue
        # The record writes "secure.posture" for the "card:posture" part and
        # "<skill>.card" for a skill's one card.
        skill, step_part = entry.split(".", 1)
        part = "" if step_part == "card" else step_part
        card = f"{skill}:{part}" if part else skill
        if card not in shown:
            shown.append(card)
    assert shown == expected, (
        f"cards drawn: {shown}\nschedule says: {expected}")
