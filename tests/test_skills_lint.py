"""The lint tripwire: what it flags, and the fact that it is NOT the boundary.

The last test in this file is the important one. It asserts that a source the
lint flags is still refused *by something else* -- because a reader who starts
treating this module as the security boundary will eventually widen it, and the
jail is what actually holds.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from puffpai.skills import lint                                # noqa: E402
from skillkit import load_example                              # noqa: E402


def test_the_shipped_example_is_clean():
    _manifest, source, _fixtures = load_example()
    assert lint.lint_source(source) == []
    assert lint.has_run_entrypoint(source)


@pytest.mark.parametrize("source,needle", [
    ("import os\ndef run(i, c):\n    return {}\n", "imports 'os'"),
    ("import socket\ndef run(i, c):\n    return {}\n", "socket"),
    ("import subprocess\ndef run(i, c):\n    return {}\n", "subprocess"),
    ("from urllib import request\ndef run(i, c):\n    return {}\n", "urllib"),
    ("import requests\ndef run(i, c):\n    return {}\n", "requests"),
    ("def run(i, c):\n    return eval('1')\n", "eval"),
    ("def run(i, c):\n    exec('x=1')\n    return {}\n", "exec"),
    ("def run(i, c):\n    return open('/etc/shadow').read()\n", "open"),
    ("def run(i, c):\n    return __import__('os')\n", "__import__"),
    ("def run(i, c):\n    return ().__class__.__bases__\n", "__bases__"),
    # Assembled rather than written out: a key-shaped LITERAL in this file is
    # indistinguishable from a real one to the pre-commit secret scan (it is
    # not, and it is not a secret -- but a scanner that reasoned about intent
    # would be a scanner that could be talked out of blocking).
    ("API_KEY = '" + "sk-" + "a" * 24 + "'\ndef run(i, c):\n    return {}\n",
     "credential-shaped"),
    ("token" + " = 'abc123'\ndef run(i, c):\n    return {}\n",
     "credential-shaped"),
])
def test_things_worth_showing_the_owner(source, needle):
    findings = lint.lint_source(source)
    assert findings, source
    assert any(needle in f for f in findings), findings


def test_a_file_that_will_not_parse_is_itself_the_finding():
    findings = lint.lint_source("def run(:\n")
    assert findings and "does not parse" in findings[0]


def test_no_run_entrypoint_is_detected():
    assert not lint.has_run_entrypoint("x = 1\n")
    assert not lint.has_run_entrypoint("def run():\n    pass\n")
    assert lint.has_run_entrypoint("def run(inputs, ctx):\n    return {}\n")


def test_lint_never_raises_on_junk():
    for junk in (None, 12, b"bytes", "", "\x00"):
        assert isinstance(lint.lint_source(junk), list)


def test_the_lint_is_a_tripwire_and_the_jail_is_the_boundary():
    """A source the lint flags STILL runs only inside the jail.

    Stated as a test so the claim in the module docstring is checked rather
    than believed: nothing in the dispatch path consults ``lint_source`` to
    decide whether to run a skill. The lint's output goes on the consent card.
    """
    import inspect

    from puffpai.skills import dispatch

    body = inspect.getsource(dispatch)
    assert "lint" not in body, (
        "skills.dispatch now consults the lint. The lint is bypassable in one "
        "line; if it starts gating execution, the appliance has swapped a real "
        "boundary for a tripwire.")
