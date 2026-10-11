"""Ops row: "can you do a system health check" runs the probe, not the library.

answered with a library explanation of what a
health check IS, citing a software guide, in 15 seconds.
"""

import pytest

from puffpai.agents import system_health as sh

REPORT_OK = {"ok": True, "healthy": 5, "total": 5, "checks": [
    {"service": "ollama", "reachable": True},
    {"service": "chromadb", "reachable": True},
    {"service": "api_server", "reachable": True},
    {"service": "nodered", "reachable": True},
    {"service": "jellyfin", "reachable": True},
]}

REPORT_DEGRADED = {"ok": True, "healthy": 3, "total": 5, "checks": [
    {"service": "ollama", "reachable": True},
    {"service": "chromadb", "reachable": False},
    {"service": "api_server", "reachable": True},
    {"service": "nodered", "reachable": False},
    {"service": "jellyfin", "reachable": True},
]}


# ── the trigger table ────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "can you do a system health check",           # the exact live phrasing
    "Can you do a system health check?",
    "do a system health check",
    "run a health check",
    "health check",
    "system health",
    "system status",
    "are all your services running",
    "are your services running?",
    "is everything running",
    "is everything working?",
])
def test_every_live_phrasing_reaches_the_probe(text):
    assert sh.is_health_question(text)


@pytest.mark.parametrize("text", [
    "what is a health check",
    "how do I do a health check on my car",
    "tell me about system health monitoring",
    "what's my health like",
    "is the living room light on",
    "log my workout",
])
def test_declines_everything_that_is_not_an_ops_question(text):
    assert not sh.is_health_question(text)
    assert sh.handle(text, REPORT_OK) is None


# ── the spoken summary ───────────────────────────────────────────────────

def test_all_up_reads_cleanly():
    reply = sh.handle("can you do a system health check", REPORT_OK,
                      {"ready": True})
    assert "All 5 services are up." in reply
    assert "The knowledge store is ready." in reply
    assert "Voice is available." in reply
    assert "No failures." in reply
    # Never the library.
    assert "guide" not in reply.lower()


def test_a_degraded_box_names_what_is_down():
    reply = sh.reply_for(REPORT_DEGRADED, {"ready": True})
    assert "3 of 5 services are up." in reply
    assert "the knowledge store" in reply and "Node-RED" in reply
    assert "The knowledge store isn't ready." in reply
    assert "2 checks failed." in reply


def test_missing_voice_models_are_named():
    reply = sh.reply_for(REPORT_OK,
                         {"ready": False, "missing": ["piper model path"]})
    assert "Voice isn't fully available" in reply
    assert "piper model path" in reply


def test_a_probe_that_could_not_run_never_reads_as_healthy():
    for report in (None, {}, {"ok": False}, "nope"):
        reply = sh.reply_for(report)
        assert "couldn't run the health probe" in reply
        assert "All" not in reply
        assert "No failures" not in reply


def test_one_failure_is_singular():
    report = {"ok": True, "checks": [
        {"service": "ollama", "reachable": True},
        {"service": "jellyfin", "reachable": False},
    ]}
    reply = sh.reply_for(report)
    assert "1 check failed." in reply


# ── the wiring ───────────────────────────────────────────────────────────

def test_it_is_registered_once_and_ahead_of_the_document_rule_and_rag():
    from tests.test_instant_answer_precedence import _call_order

    order = [name for _lineno, name in _call_order()]
    assert order.count("_try_system_health") == 1
    assert order.index("_try_system_health") < order.index("_try_document_read")
    assert order.index("_try_system_health") < order.index("_try_registry_read")


def test_the_live_path_runs_the_caretaker_probe(monkeypatch):
    from puffpai import api_server

    calls = []

    class Caretaker:
        def run_health_checks(self):
            calls.append(True)
            return REPORT_OK

    monkeypatch.setattr(api_server, "_get_agent_team",
                        lambda: {"caretaker": Caretaker()})
    reply = api_server._try_system_health("can you do a system health check")
    assert calls == [True]
    assert "All 5 services are up." in reply


def test_no_caretaker_means_an_honest_decline_not_a_clean_bill(monkeypatch):
    from puffpai import api_server

    monkeypatch.setattr(api_server, "_get_agent_team", lambda: None)
    reply = api_server._try_system_health("system status")
    assert "couldn't run the health probe" in reply
