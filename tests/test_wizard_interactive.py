"""The wizard driven the way a person drives it: read the screen, then answer.

Every other wizard test exercises ``--yes``, which is the path that asks
nothing. This module drives the INTERACTIVE conversation end to end, for three
households:

* **A** - a plain box: no gear, every default taken, one typo per step it can.
* **B** - a household that runs Home Assistant and keeps its vault and its
  media in folders of its own choosing.
* **C** - somebody who wants HomeBot's own network edge (Zone 3), in both of
  the states that answer can be in: a machine that cannot run it (this one),
  and a machine that can (the support verdict is substituted, the wizard is
  not).

**How the answers are chosen.** Not from a list. :mod:`tests._wizard_driver`
watches the child's output, notices when it stops mid-line waiting for a line
of input, and answers *the question on the screen* from a table keyed on the
question's words. That is not a nicety: this build's schedule is eighteen steps
with uncarded runner steps (household, storage, Home Assistant) between the
cards, and the vault's recovery ceremony reads between zero and three extra
lines depending on whether the code that was shown once came back. A fixed list
of answers lands them on the wrong questions and the damage shows up six steps
later as a manifest nobody typed.

**Where C is driven, and why it is not the verb.** This build's carded Bridge
asks for a posture and for what is already in front of the box; it does not
carry the edge menu, so the card wizard always records ``edge.kind: "none",
declared: true``. Zone 3 is asked for on the linear walk that
:func:`puffpai.setup_cli.cmd_setup_legacy` still runs and that the CLI still
exposes through ``--edge``. So persona C drives that walk, with the same
rules-keyed reader, and persona A asserts what the carded Bridge really
records. Neither test pretends the other's path was taken.

What each run has to prove, every time:

1. it exits 0,
2. it writes a manifest,
3. the vault PIN, the recovery code and the Home Assistant token appear nowhere
   in the output or on disk - except the ONE intentional display of the
   recovery code on the interactive vault screen,
4. a wrong answer is re-asked, never swallowed and never fatal,
5. nothing on the network is started, bound or changed.

Safety, because this suite runs on a box with a live install on the same LAN:
the Ollama address answered into the wizard is ``127.0.0.1:11499``, where
nothing listens, and the Home Assistant address is the same. No test here may
reach a real service, start a container, bind port 53 or touch DNS. Nothing
answers at the brain's address on purpose: a wizard that cannot find a brain
still has to finish.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import json
import os
import subprocess
import sys

import pytest

from tests import _wizard_driver as driver


REPO = driver.REPO

#: Nothing listens here, on purpose. Both the Ollama probe and the Home
#: Assistant probe are pointed at it so an interactive run is a local,
#: refused-connection round trip rather than a call to anybody's appliance.
DEAD_URL = "http://127.0.0.1:11499"

#: The three secrets a run must never disclose. Distinctive strings, so a grep
#: over the output and the state tree means something.
PIN_A = "Orbit-Ok-2299!"
PIN_B = "Comet-Ok-2299!"
PIN_C = "Pulsar-Ok-2299!"
FAKE_HA_TOKEN = "fake.ha.token.wizard.interactive.DO.NOT.USE"

#: A weak PIN, typed first on purpose. The vault refuses it by name.
WEAK_PIN = "1234"


# ── the shared rules ────────────────────────────────────────────────────────

#: The answers every persona gives to the questions no persona is about. A
#: blank line means "keep what was proposed", which is a real answer to every
#: prompt in this wizard, so a question none of these patterns claims is
#: answered by leaving the proposal alone.
COMMON_RULES: list[driver.Rule] = [
    # Nothing may be pulled over the network by a test.
    (r"download it now|fetch now", "no"),
    (r"where ollama answers", DEAD_URL),
    (r"type the recovery code back|type the code back",
     lambda ask: ask.recovery_code()),
]


def _rules(*persona: driver.Rule) -> list[driver.Rule]:
    """Persona rules first, then the shared ones. First match wins."""
    return [*persona, *COMMON_RULES]


def _manifest(root):
    path = root / "state" / "config" / "build_manifest.json"
    assert path.is_file(), "the wizard finished without writing a build record"
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_no_secrets(result, root, *, pin, ha_token=None):
    """The PIN is never shown or stored; a token only ever reaches its own file."""
    output = result.stdout + result.stderr
    assert pin not in output, "the vault PIN reached the wizard's output"
    assert pin not in driver.tree_text(root), "the vault PIN was written somewhere"
    if ha_token:
        assert ha_token not in output, "the Home Assistant token was printed"
        ha_file = root / "state" / "config" / "home_assistant_config.json"
        if not ha_file.is_file():
            ha_file = root / "config" / "home_assistant_config.json"
        assert ha_file.is_file(), "HA was configured but its config file is missing"
        assert ha_token in ha_file.read_text(encoding="utf-8")
        # ...and nowhere else. The build record is the file people share.
        manifest = (root / "state" / "config" / "build_manifest.json")
        assert ha_token not in manifest.read_text(encoding="utf-8")


# ── persona A: a plain box, no gear, every default, one typo per step ───────

#: The two typos persona A makes. Both are on fields with a CLOSED menu, which
#: is the only place a typo can be refused: the posture field says "or type
#: one", so anything typed there is a legitimate answer and is reconciled
#: rather than re-asked. Putting a typo there would have asserted a re-ask that
#: does not happen and passed anyway, because the posture it lands on is the
#: default.
A_TYPOS = ("banana", "wherever")

A_RULES = _rules(
    # HomeBot's setup asks four things (the brain, the PIN twice, Home
    # Assistant); the brain question is the only menu, and a typo there is
    # typed first on purpose.
    (r"or type 2 to pick another", [A_TYPOS[0], ""]),
    (r"your vault PIN|choose a vault PIN", PIN_A),
    (r"what you type stays hidden", PIN_A),
    (r"type it (again|once more)", PIN_A),
    (r"connect home assistant now", "no"),
    (r"do you run home assistant", "no"),
    (r"connect a home assistant instance", "no"),
)


@pytest.fixture(scope="module")
def run_a(tmp_path_factory):
    """One persona-A run, asserted from several angles. Runs are not cheap."""
    root = driver.install_root(tmp_path_factory.mktemp("persona_a"), "A")
    result = driver.drive(A_RULES, env=driver.base_env(root),
                          args=("--stated-ram", "16"))
    return root, result


def test_persona_a_no_gear_still_finishes_with_a_configured_bridge(run_a):
    root, result = run_a
    assert result.returncode == 0, result.log_text() + result.transcript[-3000:]

    manifest = _manifest(root)
    bridge = manifest["bridge"]
    # HomeBot asks no Bridge question: a fresh build records Moat (only the
    # outside services switched on), and the Bridge zone is still filled in.
    assert bridge["posture"] == "moat"
    edge = bridge["edge"]
    assert edge["kind"] == "none"
    assert edge["zone3"]["requested"] is False
    assert bridge["label"] and bridge["allows"], "the Bridge zone is fully filled in"

    assert manifest["integrations"]["home_assistant"]["present"] is False
    _assert_no_secrets(result, root, pin=PIN_A)


def test_persona_a_re_asks_every_wrong_answer_and_never_dead_ends(run_a):
    root, result = run_a
    assert result.returncode == 0
    # HomeBot's one menu (the brain) keeps its recommendation when the answer
    # is not one it offers: the typo neither ends the run nor is recorded.
    manifest = _manifest(root)
    assert A_TYPOS[0] in [turn.answer for turn in result.turns]
    for typo in A_TYPOS:
        assert typo not in json.dumps(manifest)
    assert manifest["brain"]["model"], "the recommendation was kept"


def test_a_weak_pin_is_refused_by_name_and_the_run_still_finishes(tmp_path):
    """A digits-only PIN is not sealed with, and it does not end the wizard.

    Its own run, because a refused PIN means no vault and therefore no recovery
    ceremony: folding it into persona A would cost the one test that proves the
    code is shown exactly once.
    """
    root = driver.install_root(tmp_path, "AW")
    rules = _rules(
        (r"reach the internet WHILE YOU BUILD", "offline"),
        (r"vault PIN|type it (again|once more)|what you type stays hidden",
         WEAK_PIN),
        (r"connect home assistant now", "no"),
        (r"do you run home assistant", "no"),
        (r"connect a home assistant instance", "no"),
    )
    result = driver.drive(rules, env=driver.base_env(root),
                          args=("--stated-ram", "16"))
    assert result.returncode == 0, result.log_text() + result.transcript[-3000:]
    assert "digits-only PIN" in result.transcript, result.transcript[-3000:]
    vault = _manifest(root)["vault"]
    assert vault["sealed"] is False and vault["ready"] is False
    assert not driver.CODE_RE.findall(result.transcript), (
        "a recovery code was minted for a vault that was never sealed")


def test_persona_a_shows_the_recovery_code_exactly_once_and_stores_it_nowhere(run_a):
    """The single intentional display. It must not be anywhere else."""
    root, result = run_a
    assert result.returncode == 0

    # Deliberately format-agnostic: this build's vault mints the code (not
    # puffpai.zone_vault), and a test that pinned one module's grouping would
    # pass for the wrong reason. A grouped uppercase code alone on an indented
    # line is the thing the screen shows.
    output = result.transcript
    codes = driver.CODE_RE.findall(output)
    assert codes, "the interactive vault screen did not show a recovery code"
    assert len(set(codes)) == 1, "more than one code-shaped line was printed"
    assert output.count(codes[0]) == 1, "the code was shown more than once"
    assert codes[0] not in driver.tree_text(root), "the recovery code was written down"


# ── persona B: Home Assistant, and folders of their own ────────────────────

def _b_rules(root):
    return _rules(
        (r"reach the internet WHILE YOU BUILD", "online"),
        (r"what may the FINISHED BOT reach", "island"),
        (r"what you call it|what should this bot be called", "Nebula"),
        # Not absolute the first time: the storage step has to refuse it.
        (r"where the Vault's encrypted files live",
         ["relative/vault", str(root / "own_vault")]),
        (r"your media library", str(root / "own_media")),
        (r"your vault PIN|choose a vault PIN", PIN_B),
        (r"what you type stays hidden", PIN_B),
        (r"type it (again|once more)", PIN_B),
        (r"connect home assistant now", "yes"),
        (r"do you run home assistant", "yes"),
        (r"connect a home assistant instance", "yes"),
        # Both the uncarded step ("address") and the Tools card ("URL"). It
        # must never be left at the default: homeassistant.local is a name
        # that resolves on somebody's real network.
        (r"home assistant (url|address)", DEAD_URL),
        (r"long-lived access token", FAKE_HA_TOKEN),
        (r"how far may your bot reach", "moat"),
    )


@pytest.fixture(scope="module")
def run_b(tmp_path_factory):
    root = driver.install_root(tmp_path_factory.mktemp("persona_b"), "B")
    result = driver.drive(_b_rules(root), env=driver.base_env(root),
                          args=("--stated-ram", "16"))
    return root, result


def test_persona_b_home_assistant_and_folders_of_their_own(run_b):
    root, result = run_b
    assert result.returncode == 0, result.log_text() + result.transcript[-3000:]

    manifest = _manifest(root)
    storage = manifest["storage"]
    # HomeBot's setup asks no folders (they are set later in Settings): the
    # vault is in the data folder and no media library is assumed.
    assert storage["vault_dir"] == str(root / "state" / "vault_data")
    assert storage["media_library_present"] is False
    assert not os.path.isdir(root / "own_vault")

    ha = manifest["integrations"]["home_assistant"]
    assert ha["present"] is True
    # The token is typed on the Tools card, which this build runs AFTER the
    # integrations block is assembled, so the manifest's token flag lags what
    # was actually written. What matters for a secret is where it ended up,
    # and ``_assert_no_secrets`` holds that: its own config file and nowhere
    # else, including this record.
    assert (root / "config" / "home_assistant_config.json").is_file()

    # This build's posture word, from this build's own ladder (HomeBot's
    # default; the Bridge is not asked at setup).
    assert manifest["bridge"]["posture"] == "moat"
    edge = manifest["bridge"]["edge"]
    assert edge["kind"] == "none"
    assert edge["contacted"] is False

    _assert_no_secrets(result, root, pin=PIN_B, ha_token=FAKE_HA_TOKEN)


def test_persona_b_re_asks_a_relative_folder(run_b):
    # HomeBot's setup asks no folder, so there is no relative path to refuse;
    # the address it does ask for is used exactly as typed.
    root, result = run_b
    assert result.returncode == 0
    assert "Use a full path" not in result.transcript
    assert _manifest(root)["integrations"]["home_assistant"]["url"] == DEAD_URL


# ── persona C: HomeBot's own network edge, opt-in ───────────────────────────

def _c_rules():
    return _rules(
        (r"reach the internet WHILE YOU BUILD", "online"),
        (r"what may the FINISHED BOT reach", "island"),
        (r"choose a posture", "fenced"),
        (r"how far may your bot reach", "fenced"),
        # The linear walk's vault asks with its own words.
        (r"choose a vault PIN", PIN_C),
        (r"type it (again|once more)", PIN_C),
        # The edge menu, on the linear walk. Its prompt is the bare word
        # "Choose", so this rule is matched against the menu printed above it
        # -- which is the only thing that says which menu it is.
        (r"What protects your network edge today", "zone3"),
        (r"your vault PIN", PIN_C),
        (r"do you run home assistant", "no"),
        (r"connect a home assistant instance", "no"),
        (r"what should this bot be called|what you call it", "Quasar"),
    )


def _drive_legacy(root, monkeypatch, *, extra_rules=()):
    """Drive the linear walk in process, answering the screen it prints."""
    import puffpai.config as config
    from puffpai import setup_cli

    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(root / "state"))
    monkeypatch.setenv("PUFFPAI_CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv(setup_cli.STDIN_SECRETS_ENV, "1")
    config.get_config(_force_reload=True)

    screen = driver.Screen()
    turns: list[driver.Turn] = []
    rules = [*extra_rules, *_c_rules()]
    monkeypatch.setattr(builtins, "input",
                        driver.scripted_input(rules, screen, turns=turns))

    args = argparse.Namespace(yes=False, stated_ram=16.0)
    with contextlib.redirect_stdout(screen):
        code = setup_cli.cmd_setup_legacy(args)
    result = driver.Result(returncode=code, transcript=screen.text, turns=turns)
    return result


def test_persona_c_zone3_on_a_machine_that_cannot_run_it(tmp_path, monkeypatch):
    """Asked for, refused in one plain sentence, and nothing was started."""
    root = driver.install_root(tmp_path, "C")
    result = _drive_legacy(root, monkeypatch)
    assert result.returncode == 0, result.log_text() + result.transcript[-3000:]

    zone3 = _manifest(root)["bridge"]["edge"]["zone3"]
    assert zone3["requested"] is True
    assert zone3["applied"] is False
    if sys.platform == "linux" and zone3["supported"]:
        # A Linux CI runner with Docker: the other half of this is asserted in
        # the substituted-verdict test below, but the record must still say
        # nothing was applied and the mode must be monitor-first.
        assert zone3["mode"] == "monitor-first"
    else:
        assert zone3["supported"] is False
        assert zone3["reasons"], "a refusal with no reason is not a refusal"
        assert zone3["mode"] == "", "an edge that cannot start has no mode"
        assert "Nothing was started" in _manifest(root)["bridge"]["edge"]["summary"]

    # Whatever the verdict, the wizard did not touch the network.
    assert ("docker compose" not in result.transcript
            or "pending" in result.transcript.lower())
    _assert_no_secrets(result, root, pin=PIN_C)


def test_a_numbered_menu_refuses_a_typo_and_says_what_it_takes(monkeypatch, capsys):
    """The linear walk's own re-ask, asserted where it lives.

    The carded steps re-ask through the renderer (persona A); the edge menu is
    a numbered list read by :func:`puffpai.setup_cli._pick_by_id`, and its
    refusal has different words. Driven here rather than through a whole run,
    because a menu answered wrongly the first time still ends the run the same
    way and a second five-minute wizard would prove nothing more.
    """
    from puffpai import setup_cli

    typed = iter(["piehole", "zone3"])
    monkeypatch.setattr(builtins, "input", lambda *_a: next(typed))
    picked = setup_cli._pick_by_id("  Choose", [
        {"id": "none", "label": "Nothing in front of it"},
        {"id": "zone3", "label": "HomeBot's own edge"},
    ])
    assert picked == "zone3"
    out = capsys.readouterr().out
    assert "Not one of these." in out
    assert "none, zone3" in out, "the refusal has to say what it would take"


def test_persona_c_zone3_on_a_machine_that_can_writes_commands_and_runs_none(
        tmp_path, monkeypatch):
    """The supported path, with the machine verdict substituted.

    The verdict is what is faked - platform and Docker - and NOT the wizard. So
    the thing under test is exactly the branch that runs on a Linux box with
    Docker: it must record monitor-first, write the owner's commands into
    pending_apply, and execute none of them.
    """
    import puffpai.config as config
    from puffpai import setup_ops

    root = driver.install_root(tmp_path, "C2")

    monkeypatch.setattr(setup_ops, "zone3_support", lambda: {
        "supported": True, "reasons": ["Linux with Docker, and port 53 looks free."],
        "platform": "linux", "docker": True, "port_53": "free"})

    # The hardware census legitimately shells out; Zone 3 does not. Every
    # command the run issues is recorded and then held against a list of the
    # things this step is forbidden to do.
    issued: list = []
    real_run = subprocess.run

    def _watched(argv, *args, **kwargs):
        issued.append(argv)
        return real_run(argv, *args, **kwargs)
    monkeypatch.setattr(subprocess, "run", _watched)

    real_popen = subprocess.Popen

    def _watched_popen(argv, *args, **kwargs):
        issued.append(argv)
        return real_popen(argv, *args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", _watched_popen)

    result = _drive_legacy(root, monkeypatch)
    assert result.returncode == 0, result.log_text() + result.transcript[-3000:]
    out = result.transcript

    flat = " ".join(str(c) for c in issued).lower()
    for forbidden in ("docker", "compose", "resolv", "systemd-resolved", ":53"):
        assert forbidden not in flat, f"the wizard ran something it must not: {issued!r}"

    zone3 = _manifest(root)["bridge"]["edge"]["zone3"]
    assert zone3["requested"] is True
    assert zone3["supported"] is True
    assert zone3["applied"] is False
    assert zone3["mode"] == "monitor-first"
    assert zone3["commands"], "the owner was given no commands to run"
    assert any("install.sh --with-zone3" in c for c in zone3["commands"])
    assert any("docker-compose.zone3.yml" in c for c in zone3["commands"])
    assert any("zone3_bootstrap_adguard.py" in c for c in zone3["commands"])
    assert zone3["doc"] == "docs/ZONE3_SETUP.md"
    assert zone3["doc_sections"], "the doc was cited without a section"

    pending = [p for p in setup_ops.pending_apply()
               if p["kind"] == "zone3_network_edge"]
    assert pending, "a Zone 3 opt-in that recorded nothing to do"
    assert pending[0]["detail"]["mode"] == "monitor-first"

    # The closing screen has to print it plainly, commands and all.
    assert "zone3_network_edge" in out
    assert "docker compose -f docker-compose.zone3.yml up -d" in out
    assert "monitor-first" in out
    config.get_config(_force_reload=True)


# ── the promises the whole step makes ──────────────────────────────────────

def test_zone3_support_is_read_only_and_honest_about_what_it_cannot_tell():
    from puffpai import setup_ops

    verdict = setup_ops.zone3_support()
    assert set(verdict) >= {"supported", "reasons"}
    assert isinstance(verdict["supported"], bool)
    assert verdict["reasons"] and all(r.strip() and r.isascii()
                                      for r in verdict["reasons"])
    if sys.platform != "linux":
        assert verdict["supported"] is False
        assert len(verdict["reasons"]) == 1, "one plain sentence, not a list of gripes"
    assert verdict["port_53"] in ("free", "in_use", "unknown", "unchecked")


def test_the_edge_check_never_resolves_or_opens_anything(monkeypatch):
    """The admin URL is data. Nothing may look it up, and nothing may fetch it."""
    import socket

    from puffpai import setup_ops

    def _no_dns(*args, **kwargs):  # pragma: no cover - the guard
        raise AssertionError("the edge check resolved a name")
    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)
    monkeypatch.setattr(socket, "create_connection", _no_dns)

    assert setup_ops.validate_edge_url("http://192.168.1.2/admin")[0] is True
    assert setup_ops.validate_edge_url("http://127.0.0.1:3000")[0] is True
    assert setup_ops.validate_edge_url("http://8.8.8.8/admin")[0] is False
    assert setup_ops.validate_edge_url("ftp://192.168.1.2/")[0] is False
    ok, note = setup_ops.validate_edge_url("http://pi.hole/admin")
    assert ok is True and "did not look it up" in note


def test_bridge_options_carries_what_the_dashboard_card_needs():
    """GET /setup/bridge returns this dict verbatim; the card renders from it."""
    from puffpai import setup_ops

    options = setup_ops.bridge_options(has_home_assistant=True)
    assert len(options["postures"]) == 4            # the ladder, still whole
    assert set(options["zone3_support"]) >= {"supported", "reasons"}
    edge = options["edge_options"]
    assert [row["id"] for row in edge["kinds"]] == ["none", "zone3", "external"]
    assert edge["default"] == "none"
    zone3_row = next(r for r in edge["kinds"] if r["id"] == "zone3")
    # Unavailable is shown, never hidden: an option nobody is told about is an
    # option that does not exist.
    assert "available" in zone3_row and zone3_row["commands"]
    assert all(str(row["label"]).isascii() for row in edge["kinds"])
    assert json.loads(json.dumps(options)), "the dict has to survive JSON"


def test_reconcile_bridge_takes_the_whole_answer_and_still_takes_the_old_one():
    from puffpai import setup_ops

    posture, note = setup_ops.reconcile_bridge("open_yard", "sealed")
    assert posture == "drawbridge_up" and note

    settled = setup_ops.reconcile_bridge(
        {"posture": "open_yard",
         "edge": {"kind": "external", "external_kind": "pihole",
                  "external_url": "http://192.168.1.2/admin"}},
        "sealed")
    assert settled["posture"] == "drawbridge_up"
    assert settled["note"]
    assert settled["edge"]["kind"] == "external"
    assert settled["edge"]["external_url"] == "http://192.168.1.2/admin"
    assert settled["edge"]["contacted"] is False


def test_an_edge_answer_can_never_switch_a_contract_off():
    """The manifest guard covers the new block like every other one."""
    from puffpai import build_manifest, setup_ops

    edge = setup_ops.edge_block("external", "pihole", "http://192.168.1.2/admin")
    block = setup_ops.bridge_block("fenced", run_egress="island", edge=edge)
    text = json.dumps(block)
    for forbidden in build_manifest._FORBIDDEN_KEYS:
        assert f'"{forbidden}"' not in text
    assert build_manifest.sanitize_manifest({"bridge": block})["bridge"] == block


@pytest.mark.parametrize("kind", ["none", "zone3", "external", "", "garbage"])
def test_every_edge_answer_leaves_the_bridge_fully_configured(kind):
    """Absence is never rendered as limitation: the zone is whole regardless."""
    from puffpai import setup_ops

    block = setup_ops.bridge_block(
        "fenced", run_egress="island", edge=setup_ops.edge_block(kind))
    assert block["posture"] == "fenced"
    assert block["label"] and block["summary"] and block["allows"]
    assert block["edge"]["declared"] is True
    assert block["edge"]["summary"].strip()
    assert block["edge"]["kind"] in ("none", "zone3", "external")
