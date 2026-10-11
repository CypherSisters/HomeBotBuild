"""``_chat_envelope_budget`` — the default-OFF chat egress opt-in (Part B).

A chat turn may carry egress only when BOTH:
  (a) the planning loop is on, AND
  (b) the owner set the explicit opt-in (PUFFPAI_CHAT_ALLOW_EGRESS=1 /
      agents.chat_allow_egress).
The default is OFF, and the loop-off behaviour is exactly as before.

Run: pytest tests/test_chat_egress_optin.py -q
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest
from unittest.mock import MagicMock

from tests.modpatch import patched_modules

_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)


@pytest.fixture(scope="module")
def server_mod():
    """Import api_server once, with the firewall backend mocked (no netsh)."""
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules({"firewall_manager":
                          MagicMock(FirewallManager=lambda: mock_fw)}):
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")
        yield mod


def _egress(mod, monkeypatch, *, loop: str, optin):
    monkeypatch.setenv("PUFFPAI_PLANNING_LOOP_ENABLED", loop)
    if optin is None:
        monkeypatch.delenv("PUFFPAI_CHAT_ALLOW_EGRESS", raising=False)
    else:
        monkeypatch.setenv("PUFFPAI_CHAT_ALLOW_EGRESS", optin)
    return mod._chat_envelope_budget()


def test_default_is_no_egress(server_mod, monkeypatch):
    # Loop on, opt-in unset — the default. Egress stays OFF.
    assert _egress(server_mod, monkeypatch, loop="1", optin=None).allow_egress \
        is False


def test_optin_off_is_no_egress(server_mod, monkeypatch):
    assert _egress(server_mod, monkeypatch, loop="1", optin="0").allow_egress \
        is False


def test_optin_and_loop_on_grants_egress(server_mod, monkeypatch):
    budget = _egress(server_mod, monkeypatch, loop="1", optin="1")
    assert budget.allow_egress is True


def test_optin_without_loop_is_no_egress(server_mod, monkeypatch):
    # Both halves are required: the opt-in alone, with the loop off, grants
    # nothing — and the loop-off branch is byte-for-byte the old behaviour.
    budget = _egress(server_mod, monkeypatch, loop="0", optin="1")
    assert budget.allow_egress is False
    assert budget.max_actions == 6 and budget.max_seconds == 20.0


def test_loop_off_default_unchanged(server_mod, monkeypatch):
    budget = _egress(server_mod, monkeypatch, loop="0", optin=None)
    assert budget.allow_egress is False
