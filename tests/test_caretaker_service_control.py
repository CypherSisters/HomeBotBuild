"""Tests for the Caretaker's real service control (orchestration Phase 3b).

Covers what stands between a caller and a running ``systemctl``: the unit-name
pattern, the allowlist, the self-restart refusal, the persisted rate limiter
(including survival across a simulated process restart), the exact argv on the
happy path, and the state_changing/Sentinel classification.

Every test that could conceivably shell out asserts that ``subprocess.run`` was
never reached.

Pure in-process; no systemd, no sudo, no sockets.
Run: pytest tests/test_caretaker_service_control.py -q
"""

from __future__ import annotations

import json
import os

import pytest

import puffpai.agents.caretaker as ct
from puffpai.agents.caretaker import (
    MAX_RESTARTS_GLOBAL,
    MAX_RESTARTS_PER_UNIT,
    MIN_RESTART_INTERVAL_SECONDS,
    RESTART_WINDOW_SECONDS,
    STATE_CHANGING_INTENTS,
    CaretakerAgent,
)
from puffpai.agents.envelope import Budget, Envelope


# ── harness ─────────────────────────────────────────────────────────

class SpyRun:
    """subprocess.run stand-in that records argv and never executes anything."""

    def __init__(self, returncode=0, stdout="", stderr="", raises=None):
        self.calls: list[tuple] = []
        self.kwargs: list[dict] = []
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append(tuple(argv))
        self.kwargs.append(kwargs)
        if self._raises is not None:
            raise self._raises

        class _Completed:
            returncode = self._returncode
            stdout = self._stdout
            stderr = self._stderr

        return _Completed()


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Point the restart ledger at a temp file for the duration of a test."""
    path = str(tmp_path / "caretaker_restarts.json")
    monkeypatch.setattr(ct, "RESTART_LEDGER_PATH", path)
    return path


@pytest.fixture
def spy(monkeypatch):
    s = SpyRun()
    monkeypatch.setattr(ct.subprocess, "run", s)
    monkeypatch.setattr(ct.platform, "system", lambda: "Linux")
    return s


def caretaker(**kw) -> CaretakerAgent:
    """A Caretaker whose 'self' unit is the api, with no helper installed."""
    kw.setdefault("config", object())
    kw.setdefault("helper_path", "/nonexistent/puffpai-service-control")
    return CaretakerAgent(**kw)


def env(intent, payload=None, *, state_changing=True, actions=3) -> Envelope:
    return Envelope(
        intent=intent,
        payload=payload or {},
        requester="owner",
        origin_agent="concierge",
        budget=Budget(max_actions=actions, max_seconds=30.0),
        state_changing=state_changing,
    )


# Names that must never reach a subprocess.
BAD_UNITS = [
    "ollama",
    "docker",
    "nftables",
    "puffpai-nodered",
    "sshd",
    "puffpai-api.service",
    "puffpai-api@1",
    "../../etc/systemd/system/evil",
    "puffpai-api/../ollama",
    "puffpai-dashboard; reboot",
    "puffpai-dashboard && rm -rf /",
    "puffpai-dashboard|nc",
    "puffpai-dashboard\nollama",
    "puffpai-dashboard ollama",
    "$(reboot)",
    "`reboot`",
    "puffpai-*",
    "PUFFPAI-DASHBOARD",
    "puffpai-",
    "-puffpai-dashboard",
    "",
    "   ",
    "puffpai-" + "a" * 200,
]

NON_STRINGS = [None, 42, 3.5, True, ["puffpai-dashboard"], {"unit": "puffpai-dashboard"}]


# ── classification ──────────────────────────────────────────────────

def test_restart_service_is_declared_state_changing():
    assert "restart_service" in STATE_CHANGING_INTENTS


def test_restart_on_a_read_only_envelope_is_refused_with_no_call(ledger, spy):
    """No Sentinel review on the envelope means no service control."""
    reply = caretaker().handle(
        env("restart_service", {"service": "puffpai-dashboard"},
            state_changing=False))
    assert reply["ok"] is False
    assert reply["requires_review"] is True
    assert spy.calls == []


def test_health_intents_stay_read_only(monkeypatch):
    """A health sweep must not need a state-changing envelope."""
    monkeypatch.setattr(ct.socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(OSError()))
    reply = caretaker().handle(env("health", state_changing=False))
    assert reply["ok"] is True


# ── allowlist + name validation ─────────────────────────────────────

@pytest.mark.parametrize("bad", BAD_UNITS)
def test_non_allowlisted_units_are_refused_without_subprocess(bad, ledger, spy):
    reply = caretaker().restart_service(bad)
    assert reply["ok"] is False
    assert reply["refused"] in ("invalid_unit", "not_allowlisted")
    assert spy.calls == []
    assert not os.path.exists(ledger)  # a refusal never spends restart budget


@pytest.mark.parametrize("bad", NON_STRINGS)
def test_non_string_service_is_refused(bad, ledger, spy):
    reply = caretaker().restart_service(bad)
    assert reply["ok"] is False and reply["refused"] == "invalid_unit"
    assert spy.calls == []


def test_denied_units_explain_themselves(ledger, spy):
    reply = caretaker().restart_service("nftables")
    assert reply["refused"] == "not_allowlisted"
    assert "firewall" in reply["error"]
    assert sorted(reply["allowlist"]) == ["puffpai-api", "puffpai-dashboard"]
    assert spy.calls == []


@pytest.mark.parametrize("given,expected", [
    ("dashboard", "puffpai-dashboard"),
    ("puffpai-dashboard", "puffpai-dashboard"),
    ("api", "puffpai-api"),
    ("api_server", "puffpai-api"),
    ("  puffpai-dashboard  ", "puffpai-dashboard"),
])
def test_normalize_unit_accepts_only_the_shapes_we_document(given, expected):
    assert CaretakerAgent.normalize_unit(given) == expected


# ── self-restart ────────────────────────────────────────────────────

def test_self_restart_is_refused(ledger, spy):
    """The api unit hosts this process; restarting it would kill the caller."""
    reply = caretaker().restart_service("puffpai-api")
    assert reply["ok"] is False
    assert reply["refused"] == "self_restart"
    assert "restart loop" in reply["error"]
    assert spy.calls == []
    assert not os.path.exists(ledger)


def test_self_restart_refusal_is_not_a_health_check_side_effect(ledger, spy,
                                                                monkeypatch):
    """A failing api probe must never turn into a restart of our own process."""
    monkeypatch.setattr(ct.socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(OSError()))
    agent = caretaker()
    report = agent.run_health_checks()
    assert report["healthy"] == 0
    assert spy.calls == []  # a health sweep never actuates anything

    # ...and if a watchdog does react to that report, the api is still refused.
    assert agent.restart_service("api_server")["refused"] == "self_restart"
    assert spy.calls == []


def test_a_split_out_caretaker_could_restart_the_api(ledger, spy):
    """The self-unit is the only thing making puffpai-api unrestartable."""
    agent = caretaker(self_unit="puffpai-caretaker")
    reply = agent.restart_service("puffpai-api")
    assert reply["ok"] is True
    assert spy.calls == [(ct.SUDO_PATH, "-n", ct.SYSTEMCTL_PATH, "restart", "puffpai-api")]


# ── happy path / argv shape ─────────────────────────────────────────

def test_restart_happy_path_argv_and_flags(ledger, spy):
    reply = caretaker().restart_service("puffpai-dashboard")
    assert reply["ok"] is True
    assert reply["unit"] == "puffpai-dashboard"
    assert spy.calls == [(ct.SUDO_PATH, "-n", ct.SYSTEMCTL_PATH, "restart", "puffpai-dashboard")]
    kwargs = spy.kwargs[0]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == ct.RESTART_TIMEOUT_SECONDS
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False


def test_helper_wrapper_is_preferred_when_installed(tmp_path, ledger, spy):
    helper = tmp_path / "puffpai-service-control"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(helper, 0o755)
    agent = caretaker(helper_path=str(helper))
    agent.restart_service("puffpai-dashboard")
    assert spy.calls == [
        (ct.SUDO_PATH, "-n", str(helper), "restart", "puffpai-dashboard")]


def test_non_linux_never_executes_anything(ledger, spy, monkeypatch):
    monkeypatch.setattr(ct.platform, "system", lambda: "Windows")
    reply = caretaker().restart_service("puffpai-dashboard")
    assert reply["ok"] is False
    assert reply["refused"] == "unsupported_platform"
    assert spy.calls == []


def test_failed_systemctl_reports_not_ok(ledger, monkeypatch):
    spy = SpyRun(returncode=5, stderr="Failed to restart")
    monkeypatch.setattr(ct.subprocess, "run", spy)
    monkeypatch.setattr(ct.platform, "system", lambda: "Linux")
    reply = caretaker().restart_service("puffpai-dashboard")
    assert reply["ok"] is False and reply["returncode"] == 5
    assert "Failed to restart" in reply["stderr"]


def test_timeout_is_reported_not_raised(ledger, monkeypatch):
    spy = SpyRun(raises=ct.subprocess.TimeoutExpired(cmd="systemctl", timeout=30))
    monkeypatch.setattr(ct.subprocess, "run", spy)
    monkeypatch.setattr(ct.platform, "system", lambda: "Linux")
    reply = caretaker().restart_service("puffpai-dashboard")
    assert reply["ok"] is False and reply["timeout"] is True


def test_intent_routes_through_handle(ledger, spy):
    reply = caretaker().handle(
        env("restart_service", {"service": "puffpai-dashboard"}))
    assert reply["ok"] is True
    assert spy.calls == [(ct.SUDO_PATH, "-n", ct.SYSTEMCTL_PATH, "restart", "puffpai-dashboard")]


# ── rate limiting ───────────────────────────────────────────────────

def test_second_restart_inside_the_minimum_interval_is_refused(ledger, spy):
    agent = caretaker()
    assert agent.restart_service("puffpai-dashboard")["ok"] is True
    reply = agent.restart_service("puffpai-dashboard")
    assert reply["ok"] is False and reply["refused"] == "rate_limited"
    assert "minimum interval" in reply["error"]
    assert len(spy.calls) == 1  # the second one never shelled out


def test_window_cap_refuses_the_fourth_restart_of_a_unit(ledger, spy, monkeypatch):
    """Age each record just past the min interval; the hourly cap still bites."""
    agent = caretaker()
    now = [1_000_000.0]
    monkeypatch.setattr(ct.time, "time", lambda: now[0])

    for _ in range(MAX_RESTARTS_PER_UNIT):
        assert agent.restart_service("puffpai-dashboard")["ok"] is True
        now[0] += MIN_RESTART_INTERVAL_SECONDS + 1

    reply = agent.restart_service("puffpai-dashboard")
    assert reply["ok"] is False and reply["refused"] == "rate_limited"
    assert f"max {MAX_RESTARTS_PER_UNIT}" in reply["error"]
    assert len(spy.calls) == MAX_RESTARTS_PER_UNIT


def test_records_outside_the_window_stop_counting(ledger, spy, monkeypatch):
    agent = caretaker()
    now = [1_000_000.0]
    monkeypatch.setattr(ct.time, "time", lambda: now[0])
    for _ in range(MAX_RESTARTS_PER_UNIT):
        agent.restart_service("puffpai-dashboard")
        now[0] += MIN_RESTART_INTERVAL_SECONDS + 1
    assert agent.restart_service("puffpai-dashboard")["refused"] == "rate_limited"

    now[0] += RESTART_WINDOW_SECONDS + 1  # everything ages out
    assert agent.restart_service("puffpai-dashboard")["ok"] is True


def test_global_cap_binds_before_any_single_unit_cap(ledger, spy, monkeypatch):
    """Alternating units must not multiply the appliance's restart budget."""
    agent = caretaker(self_unit="puffpai-caretaker")  # frees puffpai-api
    now = [1_000_000.0]
    monkeypatch.setattr(ct.time, "time", lambda: now[0])

    done = 0
    while done < MAX_RESTARTS_GLOBAL:
        for unit in ("puffpai-api", "puffpai-dashboard"):
            if done < MAX_RESTARTS_GLOBAL:
                assert agent.restart_service(unit)["ok"] is True
                done += 1
        now[0] += MIN_RESTART_INTERVAL_SECONDS + 1

    reply = agent.restart_service("puffpai-api")
    assert reply["refused"] == "rate_limited"
    assert "across all units" in reply["error"]
    # ...and it bit before either unit reached its own per-unit cap.
    saved = json.loads(open(ledger, encoding="utf-8").read())
    assert all(len(v) < MAX_RESTARTS_PER_UNIT for v in saved.values())
    assert len(spy.calls) == MAX_RESTARTS_GLOBAL


def test_a_clock_that_jumps_backwards_does_not_reset_the_limit(ledger, spy,
                                                               monkeypatch):
    agent = caretaker()
    now = [1_000_000.0]
    monkeypatch.setattr(ct.time, "time", lambda: now[0])
    assert agent.restart_service("puffpai-dashboard")["ok"] is True
    now[0] -= 86_400  # someone (or NTP) moved the clock back a day
    assert agent.restart_service("puffpai-dashboard")["refused"] == "rate_limited"
    assert len(spy.calls) == 1


def test_rate_limit_survives_a_process_restart(ledger, spy):
    """A fresh agent object (new process) must inherit the persisted history."""
    assert caretaker().restart_service("puffpai-dashboard")["ok"] is True
    assert os.path.exists(ledger)

    reborn = caretaker()  # simulates the unit coming back up
    reply = reborn.restart_service("puffpai-dashboard")
    assert reply["ok"] is False and reply["refused"] == "rate_limited"
    assert len(spy.calls) == 1


def test_ledger_records_the_attempt_before_the_restart_runs(ledger, monkeypatch):
    """A restart that kills us mid-call still has to count."""
    boom = SpyRun(raises=OSError("process died"))
    monkeypatch.setattr(ct.subprocess, "run", boom)
    monkeypatch.setattr(ct.platform, "system", lambda: "Linux")
    caretaker().restart_service("puffpai-dashboard")

    saved = json.loads(open(ledger, encoding="utf-8").read())
    assert len(saved["puffpai-dashboard"]) == 1
    assert caretaker().restart_service("puffpai-dashboard")["refused"] == "rate_limited"


def test_corrupt_ledger_reads_as_empty_but_still_writes(ledger, spy):
    with open(ledger, "w", encoding="utf-8") as fh:
        fh.write("{not json at all")
    assert caretaker().restart_service("puffpai-dashboard")["ok"] is True
    assert json.loads(open(ledger, encoding="utf-8").read())["puffpai-dashboard"]


def test_unwritable_ledger_refuses_the_restart(tmp_path, spy, monkeypatch):
    """An unenforceable rate limit is the same as no rate limit. Fail closed."""
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(ct, "RESTART_LEDGER_PATH",
                        str(blocked / "sub" / "caretaker_restarts.json"))
    reply = caretaker().restart_service("puffpai-dashboard")
    assert reply["ok"] is False and reply["refused"] == "ledger_unwritable"
    assert spy.calls == []


def test_ledger_path_resolves_through_the_state_dir(monkeypatch, tmp_path):
    """Same contract as agent_events/majordomo: $PUFFPAI_DATA_DIR wins."""
    monkeypatch.setattr(ct, "RESTART_LEDGER_PATH", None)
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    path = ct.get_restart_ledger_path()
    assert os.path.basename(path) == "caretaker_restarts.json"
    assert os.path.normcase(str(tmp_path)) in os.path.normcase(path)
    monkeypatch.setattr(ct, "RESTART_LEDGER_PATH", None)
