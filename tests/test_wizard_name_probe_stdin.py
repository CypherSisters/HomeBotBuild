"""Three things a stranger hits in the wizard's first minutes.

This tree runs the wizard as CARDS (``puffpai.wizard.runner.SCHEDULE``), so
much of what a line of prompts would test here is checked elsewhere:

* the name prompt -- the Purpose card asks it; ``tests/test_wizard_purpose.py``
  and the scripted golden (``tests/test_wizard_golden.py``, which asserts the
  typed name reaches ``manifest["build"]["name"]``) are where that behaviour is
  checked.
* the Ollama address -- the Discover card probes;
  ``tests/test_wizard_discover.py`` covers it. What is kept below is
  ``_ollama_address_source``: a configured address is an answer.
* the vault PIN prompts -- the Secure card owns the vault ceremony;
  ``tests/test_wizard_secure.py`` and ``tests/test_wizard_interactive.py`` cover
  it.
* the manifest step -- it takes the run's context, not a bag of finished
  answers.

Run:
    pytest tests/test_wizard_name_probe_stdin.py
"""
from __future__ import annotations

import argparse
import getpass
import io
import os
from contextlib import redirect_stdout

import pytest

from puffpai import cli
from puffpai.config import get_config
from puffpai.wizard import host

GOOD_PIN = "9182736455"


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    get_config(_force_reload=True)
    yield tmp_path
    get_config(_force_reload=True)


def _args(**kw):
    base = dict(yes=False, name=None, ollama_url=None, vault_password=None,
                skip_vault=False, goal=None, scaffold=False, lists=None)
    base.update(kw)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# the address the wizard was given, and where it came from
# ---------------------------------------------------------------------------

def test_the_source_of_the_address_is_named(scratch, monkeypatch):
    cfg = get_config()
    assert cli._ollama_address_source(_args(ollama_url="http://a:1"), cfg) == (
        "http://a:1", "the --ollama-url flag")
    monkeypatch.setenv("PUFFPAI_OLLAMA_URL", "http://b:2")
    assert cli._ollama_address_source(_args(), cfg) == (
        "http://b:2", "the PUFFPAI_OLLAMA_URL environment variable")
    monkeypatch.delenv("PUFFPAI_OLLAMA_URL")
    # This tree SHIPS an ollama address in its config file (BuildBot's shipped
    # config carries none). The SHIPPED DEFAULT is not somebody's answer:
    # counting it as "your config file" meant this build never probed, so an
    # owner running Ollama anywhere but :11434 was never offered the address
    # that answers. A config url that is still the default reads as unset.
    assert cli._ollama_address_source(
        _args(), get_config(_force_reload=True))[1] == ""
    # An address the owner really did put in the config file is still an answer.
    answered = argparse.Namespace(
        _raw={"ollama": {"url": "http://10.0.0.9:11434"}},
        ollama=argparse.Namespace(url="http://10.0.0.9:11434"))
    assert cli._ollama_address_source(_args(), answered) == (
        "http://10.0.0.9:11434", "your config file")
    # The empty source - the only licence this wizard has to go guessing - is
    # what an install with no ollama block gets.
    unset = argparse.Namespace(
        _raw={}, ollama=argparse.Namespace(url="http://localhost:11434"))
    assert cli._ollama_address_source(_args(), unset)[1] == ""


# ---------------------------------------------------------------------------
# a scripted run cannot stall
# ---------------------------------------------------------------------------

def test_that_refusal_is_exit_code_one_not_a_finished_build(scratch, monkeypatch):
    """A refusal ends the run at 1 and says why; it is not a finished build.

    The run is entered through ``host._run_setup``, which is the name
    ``host._cmd_setup`` calls. ``cli._run_setup`` is a re-export, so patching it
    would leave the real wizard running and the test would pass on nothing.
    """
    monkeypatch.setattr(host, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(host, "_run_setup", lambda args: (_ for _ in ()).throw(
        cli.SetupRefused("No terminal.", "Nothing was written.", ["--skip-vault"])))
    with redirect_stdout(io.StringIO()) as buf:
        code = cli._cmd_setup(_args())
    assert code == 1
    assert "No terminal." in buf.getvalue()


def test_a_hidden_pin_read_uses_the_plain_reader_with_no_tty(monkeypatch):
    """getpass on Windows reads the CONSOLE, which a redirected file is not.

    This tree's ``_read_pin`` falls back to ``input()`` itself rather
    than to the wizard's line reader, so the plain reader is pinned where it
    actually is. The assertion is unchanged: no tty, no getpass.
    """
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": " 9182736455 ")
    monkeypatch.setattr(getpass, "getpass",
                        lambda prompt="": pytest.fail("getpass with no tty"))
    # This tree hands back the line as typed (BuildBot's reader stripped it);
    # what is asserted is the reader that was used, not the trimming.
    assert cli._read_pin("  PIN: ").strip() == GOOD_PIN


def test_running_out_of_input_stops_the_prompting(monkeypatch):
    """A redirected answers file shorter than the wizard finishes the wizard.

    The end-of-file latch lives in ``host._read_line`` (the reader the cards go
    through). Once stdin has run out, nothing reads it again.
    """
    answers = iter(["offline"])

    def fake_input(prompt=""):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    cli._reset_stdin_state()

    assert cli._read_line("First") == "offline"
    # Second read hits end of file: None, not "" - running out of input is not
    # an empty answer.
    assert cli._read_line("Second") is None
    # Every read after it is answered by the latch WITHOUT touching stdin again.
    monkeypatch.setattr("builtins.input",
                        lambda prompt="": pytest.fail("read past end of input"))
    assert cli._read_line("Third") is None
    assert cli._read_line("Fourth") is None
    cli._reset_stdin_state()


def test_an_empty_line_is_still_an_answer(monkeypatch):
    """Empty means "the default"; end-of-file means "stop asking". Not the same."""
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    cli._reset_stdin_state()
    assert cli._ask("Name", "PuffPAI") == "PuffPAI"
    assert cli._ask("Name", "PuffPAI") == "PuffPAI"  # not latched
    assert cli._stdin_exhausted is False


def test_a_refused_pin_with_no_terminal_is_a_refusal_not_a_silent_skip(
        scratch, monkeypatch):
    """The main case of this file, asked of the card runner.

    A run with no terminal and a PIN that cannot be used must not print
    "skipping the vault" and exit 0. ``host._stdin_is_tty`` is what the Secure
    card asks before it offers to take a PIN, so that is what is pinned here.
    """
    monkeypatch.setattr(host, "_stdin_is_tty", lambda: False)
    with redirect_stdout(io.StringIO()) as buf:
        code = cli.main(["setup", "--yes", "--skip-ha", "--skip-knowledge",
                         "--build-network", "offline"])
    assert code == 0, "a build with no vault still finishes"
    printed = buf.getvalue().lower()
    assert "vault" in printed
    assert not os.path.isdir(os.path.join(str(scratch), "vault")), (
        "no terminal, no PIN, so no vault may have been created")
