"""What the console wizard looks like to somebody using it for the first time.

Four things a first-time user would trip over, each one a line of output or a
question that is not a question:

* questions must not run together on one visual line (``input(prompt)`` writes
  no newline and an Enter-only answer adds none either);
* a secret field must not ask a Windows console handle when stdin is a pipe,
  and wait forever (the subprocess proof is in ``test_wizard_piped_stdin.py``);
* the Brain card must not take any word as a model name;
* a card's heading must not put the skill id level with the owner's words.

Run:
    pytest tests/test_wizard_console_ux.py
"""
from __future__ import annotations

import builtins
import getpass

import pytest

from puffpai.wizard import cards, render_cli

CARD = {
    "skill": "purpose", "section": "persona", "part": "place",
    "title": "A place of its own",
    "decided": "Stargazer - a night sky bot - its own place, as 'stargazer'",
    "status": "ready", "consequential": False,
    "estimate": {"bytes": 0, "seconds": 0, "basis": "measured"},
    "fields": [
        {"id": "scaffold", "kind": "toggle", "label": "A place of its own",
         "current": True},
        {"id": "id", "kind": "text", "label": "The id it lives under",
         "current": "stargazer", "max_length": 32},
    ],
}


def test_the_heading_is_the_owners_words_and_the_id_is_after_them():
    lines = render_cli.card_lines(cards.validate_card(CARD))
    assert lines[0].startswith("A place of its own")
    assert lines[0].rstrip().endswith("[purpose:place]")
    assert lines[1] == "=" * len("A place of its own")
    assert not lines[0].startswith("purpose"), "the id is not the label"


def test_each_question_gets_its_own_line(capsys, monkeypatch):
    """Two questions answered with Enter do not print as one run-on line."""
    typed = []
    monkeypatch.setattr(builtins, "input", lambda prompt="": typed.append(prompt) or "")
    render_cli.render(cards.validate_card(CARD), non_interactive=False)
    out = capsys.readouterr().out.splitlines()

    for field in ("A place of its own", "The id it lives under"):
        assert any(line.strip().startswith(field) and line.strip().endswith(
            "(blank to keep)") for line in out), field
    # And what the owner types after is a short marker, not the question again.
    assert typed and all(mark == render_cli.ANSWER_MARK for mark in typed)


def test_a_secret_on_a_pipe_is_read_without_asking_a_console(monkeypatch,
                                                             capsys):
    def never(*args, **kwargs):
        raise AssertionError("getpass was asked for a secret with no terminal")

    monkeypatch.setattr(getpass, "getpass", never)
    monkeypatch.setattr(render_cli, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "  typed  ")
    assert render_cli._read_secret("  Your vault PIN") == "  typed  "
    assert "Your vault PIN" in capsys.readouterr().out


def test_end_of_file_on_a_pipe_is_no_answer_and_never_a_hang(monkeypatch):
    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr(render_cli, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(builtins, "input", eof)
    assert render_cli._read_secret("  Your vault PIN") is None
    assert render_cli._read_line("  Anything") is None


def test_a_terminal_still_gets_getpass(monkeypatch):
    asked = []
    monkeypatch.setattr(render_cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(getpass, "getpass",
                        lambda prompt="": asked.append(prompt) or "1234")
    monkeypatch.setattr(builtins, "input",
                        lambda prompt="": pytest.fail("echoed on a terminal"))
    assert render_cli._read_secret("  Your vault PIN") == "1234"
    assert asked == [render_cli.ANSWER_MARK]


# -- the Brain card's model field ---------------------------------------------

@pytest.mark.parametrize("bad", ["Stargazer", "stargazer", "a night sky bot",
                                 "llama3 2", ":8b"])
def test_a_word_is_not_a_model_name(bad):
    from puffpai.wizard.packages.brain import skill as brain
    import re

    assert not re.match(brain.MODEL_PATTERN, bad), bad


@pytest.mark.parametrize("good", ["llama3.2:3b", "qwen3:30b-a3b",
                                  "library/llama3.2:3b"])
def test_a_model_name_with_its_tag_is_accepted(good):
    from puffpai.wizard.packages.brain import skill as brain
    import re

    assert re.match(brain.MODEL_PATTERN, good), good


# -- the goal that reached nothing --------------------------------------------

def test_context_names_the_command_that_would_actually_help(monkeypatch,
                                                            tmp_path):
    """A goal on the card and nothing on disk is fixed by --scaffold.

    ``puffpai setup`` on its own would record the same nothing a second time.
    """
    from puffpai import build_context
    from puffpai.wizard import fixes, loader
    from puffpai.wizard import runner as wizard_runner
    from puffpai.config import get_config

    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUFFPAI_STATE_DIR", str(tmp_path))
    cfg = get_config(_force_reload=True)
    monkeypatch.setattr(build_context, "collect_goal", lambda: "")

    packages, _refusals = loader.load_builtin_packages()
    package = packages["context"]
    module = loader.load_module(package)
    ctx = wizard_runner.RunContext(
        args=None, cfg=cfg, non_interactive=True, shared={},
        sections={"context": {"values": {"goal": "a night sky bot", "bot_name": ""}}},
        package=package)
    verdict = module.verify(ctx)
    assert verdict["status"] == "gap"
    assert verdict["gap"]["fix"]["id"] == "setup.scaffold"
    assert fixes.render(verdict["gap"]["fix"]) == "puffpai setup --scaffold"
    get_config(_force_reload=True)
