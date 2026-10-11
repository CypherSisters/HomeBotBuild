"""``workspace_exec`` — the review gate, the fail-closed path, and the jail.

Split deliberately into two halves. The gates that must hold *everywhere* run on
every platform, because they are pure policy: an unreviewed envelope is refused
and a missing sandbox refuses rather than degrading. The isolation assertions
need a real bubblewrap and so skip where there is none — but they are the point
of the tool, so they are written as escape *attempts* rather than as a check
that the happy path returns ok.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""
from __future__ import annotations

import os

import pytest

from puffpai.agents.envelope import Budget, Envelope
from puffpai.agents.toolsmith import (
    BWRAP,
    EXEC_MAX_OUTPUT_BYTES,
    EXEC_MAX_TIMEOUT,
    STATE_CHANGING_INTENTS,
    TOOL_INTENTS,
    ToolsmithAgent,
)

have_bwrap = os.path.isfile(BWRAP) and os.access(BWRAP, os.X_OK)
needs_jail = pytest.mark.skipif(not have_bwrap,
                                reason=f"no sandbox helper at {BWRAP}")


@pytest.fixture
def agent(tmp_path):
    return ToolsmithAgent(workspace_root=str(tmp_path))


def env(command, *, state_changing=True, timeout=None, actions=4, seconds=180.0):
    payload = {"command": command}
    if timeout is not None:
        payload["timeout"] = timeout
    return Envelope(intent="workspace_exec", payload=payload,
                    budget=Budget(max_actions=actions, max_seconds=seconds,
                                  allow_egress=False),
                    state_changing=state_changing, origin_agent="concierge")


# ── policy: holds on every platform ─────────────────────────────────

def test_exec_is_declared_state_changing():
    """The envelope minter reads this set; if exec drops out of it, an
    unreviewed command stops being refused."""
    assert "workspace_exec" in STATE_CHANGING_INTENTS
    assert "workspace_exec" in TOOL_INTENTS


def test_unreviewed_envelope_is_refused(agent):
    """state_changing=False means it never passed Drawbridge in ``bus.send``."""
    reply = agent.handle(env("echo nope", state_changing=False))
    assert reply["ok"] is False
    assert reply["requires_review"] is True


def test_exec_has_no_egress_intent():
    """Egress is absent, not gated: the jail unshares the network namespace.
    If exec were ever added to EGRESS_INTENTS that would imply a network the
    sandbox does not have."""
    from puffpai.agents.toolsmith import EGRESS_INTENTS
    assert "workspace_exec" not in EGRESS_INTENTS


def test_missing_sandbox_refuses_and_never_runs_on_the_host(agent, monkeypatch):
    """The property that matters most: losing isolation costs the capability,
    not the isolation. A regression here would run agent-authored shell on the
    host as the service account."""
    monkeypatch.setattr("puffpai.agents.toolsmith.BWRAP", "/nonexistent/bwrap")
    ran = []
    monkeypatch.setattr("puffpai.agents.toolsmith.subprocess.run",
                        lambda *a, **k: ran.append(a) or (_ for _ in ()).throw(
                            AssertionError("must not exec")))
    reply = agent.handle(env("touch /tmp/should-never-exist"))
    assert reply["ok"] is False
    assert reply["sandbox_unavailable"] is True
    assert ran == []


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_command_must_be_a_non_empty_string(agent, bad):
    payload = {} if bad is None else {"command": bad}
    reply = agent.handle(Envelope(
        intent="workspace_exec", payload=payload,
        budget=Budget(max_actions=2, max_seconds=30.0, allow_egress=False),
        state_changing=True, origin_agent="concierge"))
    assert reply["ok"] is False


@pytest.mark.parametrize("bad", ["abc", 0, -1, float("nan")])
def test_bad_timeout_is_refused(agent, bad):
    reply = agent.handle(env("echo x", timeout=bad))
    assert reply["ok"] is False


def test_timeout_is_clamped_not_honoured_verbatim(agent):
    assert agent._exec_timeout(9999) == EXEC_MAX_TIMEOUT
    assert agent._exec_timeout(None) > 0


# ── isolation: needs a real jail ────────────────────────────────────

@needs_jail
def test_runs_in_the_workspace_and_writes_reach_the_host(agent, tmp_path):
    reply = agent.handle(env("pwd; printf 'a\\nb\\n' > note.txt"))
    assert reply["ok"] is True
    assert "/work" in reply["stdout"]
    assert (tmp_path / "note.txt").is_file()
    assert reply["untrusted"] is True and reply["sandboxed"] is True


@needs_jail
@pytest.mark.parametrize("probe", [
    "cat /etc/shadow",          # host credentials
    "ls /opt",                  # the appliance install
    "ls /data",                 # the state dir, vault included
    "ls -la /var/run/docker.sock",   # root-equivalent socket
    "cat /work/../../../etc/hostname",
])
def test_host_filesystem_is_not_reachable(agent, probe):
    reply = agent.handle(env(f"{probe} 2>&1"))
    assert "No such file" in reply["stdout"], reply


@needs_jail
def test_no_secrets_leak_through_the_environment(agent):
    out = agent.handle(env("env"))["stdout"]
    assert "TOKEN" not in out and "KEY" not in out and "SECRET" not in out


@needs_jail
def test_the_network_is_absent(agent):
    """Not 'egress is denied' — there is no interface to deny."""
    dns = agent.handle(env(
        "getent hosts github.com >/dev/null 2>&1 && echo REACHABLE || echo BLOCKED"))
    assert "BLOCKED" in dns["stdout"]
    ifaces = agent.handle(env("ip -o addr show 2>&1 || cat /proc/net/dev"))["stdout"]
    # loopback carries two inet lines (v4 + v6); anything else means a real NIC.
    assert "eno" not in ifaces and "eth" not in ifaces and "wlan" not in ifaces


@needs_jail
def test_a_runaway_command_is_killed(agent):
    reply = agent.handle(env("sleep 60", timeout=2))
    assert reply["timed_out"] is True and reply["ok"] is False


@needs_jail
def test_output_is_truncated_rather_than_unbounded(agent):
    reply = agent.handle(env("yes abcdefgh | head -c 900000"))
    assert reply["truncated"] is True
    assert len(reply["stdout"]) <= EXEC_MAX_OUTPUT_BYTES


@needs_jail
def test_a_failing_command_is_a_result_not_an_exception(agent):
    reply = agent.handle(env("echo to-stderr >&2; exit 3"))
    assert reply["ok"] is False and reply["exit_code"] == 3
    assert "to-stderr" in reply["stderr"]


# ── document_convert ────────────────────────────────────────────────

def doc_env(path, to, *, state_changing=True, timeout=None):
    payload = {"path": path, "to": to}
    if timeout is not None:
        payload["timeout"] = timeout
    return Envelope(intent="document_convert", payload=payload,
                    budget=Budget(max_actions=4, max_seconds=300.0,
                                  allow_egress=False),
                    state_changing=state_changing, origin_agent="concierge")


def test_document_convert_is_state_changing_and_known():
    from puffpai.agents.toolsmith import EGRESS_INTENTS
    assert "document_convert" in STATE_CHANGING_INTENTS
    assert "document_convert" in TOOL_INTENTS
    assert "document_convert" not in EGRESS_INTENTS


def test_document_convert_refuses_unreviewed(agent):
    reply = agent.handle(doc_env("a.txt", "pdf", state_changing=False))
    assert reply["ok"] is False and reply["requires_review"] is True


@pytest.mark.parametrize("bad", ["exe", "sh", "", "../etc/passwd", "docx;rm -rf /"])
def test_only_allow_listed_target_formats(agent, tmp_path, bad):
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    assert agent.handle(doc_env("a.txt", bad))["ok"] is False


def test_document_convert_fails_closed_without_a_sandbox(agent, tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    monkeypatch.setattr("puffpai.agents.toolsmith.BWRAP", "/nonexistent/bwrap")
    monkeypatch.setattr("puffpai.agents.toolsmith.subprocess.run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not run unsandboxed")))
    reply = agent.handle(doc_env("a.txt", "pdf"))
    assert reply["ok"] is False and reply["sandbox_unavailable"] is True


def test_missing_source_is_reported_not_raised(agent):
    assert agent.handle(doc_env("nope.txt", "pdf"))["ok"] is False


@needs_jail
@pytest.mark.parametrize("fmt", ["docx", "pdf", "odt"])
def test_real_conversion_round_trip(agent, tmp_path, fmt):
    (tmp_path / "note.txt").write_text("Hello from PuffPAI.\n", encoding="utf-8")
    reply = agent.handle(doc_env("note.txt", fmt, timeout=240))
    assert reply["ok"] is True, reply
    out = tmp_path / f"note.{fmt}"
    assert out.is_file() and out.stat().st_size > 0
    assert reply["format"] == fmt and reply["untrusted"] is True


@needs_jail
def test_docx_converts_back_to_readable_text(agent, tmp_path):
    (tmp_path / "src.txt").write_text("Round trip works.\n", encoding="utf-8")
    assert agent.handle(doc_env("src.txt", "docx", timeout=240))["ok"] is True
    (tmp_path / "src.txt").unlink()
    assert agent.handle(doc_env("src.docx", "txt", timeout=240))["ok"] is True
    assert "Round trip works." in (tmp_path / "src.txt").read_text(
        encoding="utf-8", errors="replace")


@needs_jail
def test_conversion_still_has_no_network(agent, tmp_path):
    """The wider bind set must not have re-introduced egress."""
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    agent.handle(doc_env("a.txt", "pdf", timeout=240))
    probe = agent.handle(env(
        "getent hosts github.com >/dev/null 2>&1 && echo REACHABLE || echo BLOCKED"))
    assert "BLOCKED" in probe["stdout"]


@needs_jail
def test_shadow_still_unreachable_with_document_binds(agent, tmp_path):
    """DOC_RO_BINDS adds /etc/passwd but must never add /etc/shadow."""
    from puffpai.agents.toolsmith import DOC_RO_BINDS
    assert "/etc/shadow" not in DOC_RO_BINDS
    assert not any(b == "/etc" for b in DOC_RO_BINDS)


@needs_jail
def test_conversion_of_a_file_in_a_subdirectory_lands_beside_it(agent, tmp_path):
    """LibreOffice writes to its CWD unless told otherwise, so without an
    explicit --outdir a file in a subdirectory converts into the workspace root
    and the caller is wrongly told nothing was produced."""
    sub = tmp_path / "demo"
    sub.mkdir()
    (sub / "q.txt").write_text("Quarterly notes.\n", encoding="utf-8")
    reply = agent.handle(doc_env("demo/q.txt", "docx", timeout=240))
    assert reply["ok"] is True, reply
    assert (sub / "q.docx").is_file(), sorted(p.name for p in tmp_path.rglob("*"))
    assert not (tmp_path / "q.docx").exists()   # must NOT land in the root
    assert reply["path"].replace("\\", "/") == "demo/q.docx"
