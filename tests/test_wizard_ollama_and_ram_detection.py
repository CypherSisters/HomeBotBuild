"""BB-CEN-05 / BB-CEN-04 — detection that fails must recover, not shrug.

WRECK §B2, verbatim: "when *RAM* detection fails the wizard asks the owner and
accepts a typed answer (BB-CEN-02, CONFIRMED twice). When *Ollama* detection
fails it shrugs and proceeds." Table row 7: "Ollama on a non-default port:
wizard detects failure, never asks for a URL, writes :11434 anyway."

And the nitpick that is really a validation hole: "stated-RAM accepts -5 and
999999" — the figure every model on the brain menu is fitted against.

Run:
    pytest tests/test_wizard_ollama_and_ram_detection.py
"""
from __future__ import annotations

import argparse
import os

import pytest

from puffpai import brain_health, cli, hardware_census, setup_ops
from puffpai.config import get_config


@pytest.fixture
def scratch(tmp_path):
    prev = os.environ.get("PUFFPAI_DATA_DIR")
    os.environ["PUFFPAI_DATA_DIR"] = str(tmp_path)
    get_config(_force_reload=True)
    try:
        yield tmp_path
    finally:
        if prev is not None:
            os.environ["PUFFPAI_DATA_DIR"] = prev
        else:
            os.environ.pop("PUFFPAI_DATA_DIR", None)
        get_config(_force_reload=True)


def _args(**kw):
    base = dict(yes=False, ollama_url=None, stated_ram=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _reachable(monkeypatch, mapping):
    """Only the URLs in ``mapping`` answer; everything else is dead."""
    monkeypatch.setattr(setup_ops, "list_models",
                        lambda url=None, timeout=3: mapping.get(url))


# ── BB-CEN-05: finding an Ollama that is not at the default address ─────────

def test_url_shapes():
    assert brain_health.valid_url("localhost:11434") == "http://localhost:11434"
    assert brain_health.valid_url("http://box:11434/") == "http://box:11434"
    assert brain_health.valid_url("") is None
    assert brain_health.valid_url("   ") is None
    assert brain_health.valid_url("ftp://box:11434") is None
    assert brain_health.valid_url("http://") is None
    assert brain_health.valid_url("not-a-url") is None
    assert brain_health.valid_url("192.168.1.5:11434") == "http://192.168.1.5:11434"
    assert brain_health.valid_url("http://host") == "http://host"


# ── BB-CEN-04: a stated RAM figure is checked ───────────────────────────────

@pytest.mark.parametrize("bad", ["-5", "999999", "0", "abc", "-0.5", "nan"])
def test_a_bad_stated_ram_is_refused(bad):
    value, reason = cli._valid_ram_gb(bad)
    assert value is None
    assert reason, f"{bad!r} was refused with no explanation"


@pytest.mark.parametrize("good,expected", [("8", 8.0), ("61.4", 61.4),
                                           ("4096", 4096.0), ("0.5", 0.5)])
def test_a_good_stated_ram_is_accepted(good, expected):
    value, reason = cli._valid_ram_gb(good)
    assert value == expected
    assert reason == ""

